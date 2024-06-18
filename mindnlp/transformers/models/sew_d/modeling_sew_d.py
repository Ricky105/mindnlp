# Copyright 2024 Huawei Technologies Co., Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Mindspore SEW model."""

import math
import warnings
from collections.abc import Sequence
from typing import Optional, Tuple, Union
import numpy as np


import mindspore
from mindspore import nn, ops
from mindspore.common.initializer import initializer, Normal
from mindnlp.modules.weight_norm import weight_norm
from mindnlp.utils import logging
from mindnlp.modules.functional import finfo

from ...modeling_utils import PreTrainedModel
from .configuration_sew_d import SEWDConfig
from ...modeling_outputs import (
    BaseModelOutput,
    CausalLMOutput,
    SequenceClassifierOutput,
)
from ...activations import ACT2FN


logger = logging.get_logger(__name__)

_HIDDEN_STATES_START_POSITION = 1


# Copied from transformers.models.wav2vec2.modeling_wav2vec2._compute_mask_indices
def _compute_mask_indices(
    shape: Tuple[int, int],
    mask_prob: float,
    mask_length: int,
    attention_mask: Optional[mindspore.Tensor] = None,
    min_masks: int = 0,
) -> np.ndarray:
    """
    Computes random mask spans for a given shape. Used to implement [SpecAugment: A Simple Data Augmentation Method for
    ASR](https://arxiv.org/abs/1904.08779). Note that this method is not optimized to run on TPU and should be run on
    CPU as part of the preprocessing during training.

    Args:
        shape: The shape for which to compute masks. This should be of a tuple of size 2 where
               the first element is the batch size and the second element is the length of the axis to span.
        mask_prob:  The percentage of the whole axis (between 0 and 1) which will be masked. The number of
                    independently generated mask spans of length `mask_length` is computed by
                    `mask_prob*shape[1]/mask_length`. Note that due to overlaps, `mask_prob` is an upper bound and the
                    actual percentage will be smaller.
        mask_length: size of the mask
        min_masks: minimum number of masked spans
        attention_mask: A (right-padded) attention mask which independently shortens the feature axis of
                        each batch dimension.
    """
    batch_size, sequence_length = shape

    if mask_length < 1:
        raise ValueError("`mask_length` has to be bigger than 0.")

    if mask_length > sequence_length:
        raise ValueError(
            f"`mask_length` has to be smaller than `sequence_length`, but got `mask_length`: {mask_length}"
            f" and `sequence_length`: {sequence_length}`"
        )

    # epsilon is used for probabilistic rounding
    epsilon = np.random.rand(1).item()

    def compute_num_masked_span(input_length):
        """Given input length, compute how many spans should be masked"""
        num_masked_span = int(mask_prob * input_length / mask_length + epsilon)
        num_masked_span = max(num_masked_span, min_masks)

        # make sure num masked span <= sequence_length
        if num_masked_span * mask_length > sequence_length:
            num_masked_span = sequence_length // mask_length

        # make sure num_masked span is also <= input_length - (mask_length - 1)
        if input_length - (mask_length - 1) < num_masked_span:
            num_masked_span = max(input_length - (mask_length - 1), 0)

        return num_masked_span

    # compute number of masked spans in batch
    input_lengths = (
        attention_mask.sum(-1).detach().tolist()
        if attention_mask is not None
        else [sequence_length for _ in range(batch_size)]
    )

    # SpecAugment mask to fill
    spec_aug_mask = np.zeros((batch_size, sequence_length), dtype=bool)
    spec_aug_mask_idxs = []

    max_num_masked_span = compute_num_masked_span(sequence_length)

    if max_num_masked_span == 0:
        return spec_aug_mask

    for input_length in input_lengths:
        # compute num of masked spans for this input
        num_masked_span = compute_num_masked_span(input_length)

        # get random indices to mask
        spec_aug_mask_idx = np.random.choice(
            np.arange(input_length - (mask_length - 1)), num_masked_span, replace=False
        )

        # pick first sampled index that will serve as a dummy index to pad vector
        # to ensure same dimension for all batches due to probabilistic rounding
        # Picking first sample just pads those vectors twice.
        if len(spec_aug_mask_idx) == 0:
            # this case can only happen if `input_length` is strictly smaller then
            # `sequence_length` in which case the last token has to be a padding
            # token which we can use as a dummy mask id
            dummy_mask_idx = sequence_length - 1
        else:
            dummy_mask_idx = spec_aug_mask_idx[0]

        spec_aug_mask_idx = np.concatenate(
            [
                spec_aug_mask_idx,
                np.ones(max_num_masked_span - num_masked_span, dtype=np.int32)
                * dummy_mask_idx,
            ]
        )
        spec_aug_mask_idxs.append(spec_aug_mask_idx)

    spec_aug_mask_idxs = np.array(spec_aug_mask_idxs)

    # expand masked indices to masked spans
    spec_aug_mask_idxs = np.broadcast_to(
        spec_aug_mask_idxs[:, :, None], (batch_size, max_num_masked_span, mask_length)
    )
    spec_aug_mask_idxs = spec_aug_mask_idxs.reshape(
        batch_size, max_num_masked_span * mask_length
    )

    # add offset to the starting indexes so that indexes now create a span
    offsets = np.arange(mask_length)[None, None, :]
    offsets = np.broadcast_to(
        offsets, (batch_size, max_num_masked_span, mask_length)
    ).reshape(batch_size, max_num_masked_span * mask_length)
    spec_aug_mask_idxs = spec_aug_mask_idxs + offsets

    # ensure that we cannot have indices larger than sequence_length
    if spec_aug_mask_idxs.max() > sequence_length - 1:
        spec_aug_mask_idxs[spec_aug_mask_idxs > sequence_length - 1] = (
            sequence_length - 1
        )

    # scatter indices to mask
    np.put_along_axis(spec_aug_mask, spec_aug_mask_idxs, 1, -1)

    return spec_aug_mask


# Copied from transformers.models.deberta_v2.modeling_deberta_v2.make_log_bucket_position
def make_log_bucket_position(relative_pos, bucket_size, max_position):
    sign = ops.sign(relative_pos)
    mid = bucket_size // 2
    abs_pos = ops.where(
        (relative_pos < mid) & (relative_pos > -mid),
        mindspore.Tensor(mid - 1).type_as(relative_pos),
        ops.abs(relative_pos),
    )
    log_pos = (
        ops.ceil(
            ops.log(mindspore.tensor((abs_pos / mid).to(mindspore.float32)))
            / ops.log(mindspore.tensor((max_position - 1) / mid).to(mindspore.float32))
            * (mid - 1)
        )
        + mid
    )
    bucket_pos = ops.where(
        abs_pos <= mid, relative_pos.type_as(log_pos), log_pos * sign
    )
    return bucket_pos


# Copied from transformers.models.deberta_v2.modeling_deberta_v2.build_relative_position
def build_relative_position(query_size, key_size, bucket_size=-1, max_position=-1):
    """
    Build relative position according to the query and key

    We assume the absolute position of query \\(P_q\\) is range from (0, query_size) and the absolute position of key
    \\(P_k\\) is range from (0, key_size), The relative positions from query to key is \\(R_{q \\rightarrow k} = P_q -
    P_k\\)

    Args:
        query_size (int): the length of query
        key_size (int): the length of key
        bucket_size (int): the size of position bucket
        max_position (int): the maximum allowed absolute position

    Return:
        `mindspore.Tensor`: A tensor with shape [1, query_size, key_size]
    """

    q_ids = ops.arange(0, query_size)
    k_ids = ops.arange(0, key_size)
    rel_pos_ids = q_ids[:, None] - k_ids[None, :]
    if bucket_size > 0 and max_position > 0:
        rel_pos_ids = make_log_bucket_position(rel_pos_ids, bucket_size, max_position)
    rel_pos_ids = rel_pos_ids.to(mindspore.int64)
    rel_pos_ids = rel_pos_ids[:query_size, :]
    rel_pos_ids = rel_pos_ids.unsqueeze(0)
    return rel_pos_ids


def c2p_dynamic_expand(c2p_pos, query_layer, relative_pos):
    return c2p_pos.broadcast_to(
        (
            [
                query_layer.shape[0],
                query_layer.shape[1],
                query_layer.shape[2],
                relative_pos.shape[-1],
            ]
        )
    )


def p2c_dynamic_expand(c2p_pos, query_layer, key_layer):
    return c2p_pos.broadcast_to(
        (
            [
                query_layer.shape[0],
                query_layer.shape[1],
                key_layer.shape[-2],
                key_layer.shape[-2],
            ]
        )
    )


def pos_dynamic_expand(pos_index, p2c_att, key_layer):
    return pos_index.broadcast_to(
        (p2c_att.shape[:2] + (pos_index.shape[-2], key_layer.shape[-2]))
    )


# Copied from transformers.models.deberta.modeling_deberta.get_mask
def get_mask(input, local_context):
    if not isinstance(local_context, DropoutContext):
        dropout = local_context
        mask = None
    else:
        dropout = local_context.dropout
        dropout *= local_context.scale
        mask = local_context.mask if local_context.reuse_mask else None

    if dropout > 0 and mask is None:
        mask = (1 - ops.bernoulli(input=ops.zeros_like(input), p=1 - dropout)).to(
            mindspore.bool_
        )

    if isinstance(local_context, DropoutContext):
        if local_context.mask is None:
            local_context.mask = mask

    return mask, dropout


# Copied from transformers.models.wav2vec2.modeling_wav2vec2.Wav2Vec2NoLayerNormConvLayer with Wav2Vec2->SEWD
class SEWDNoLayerNormConvLayer(nn.Cell):
    def __init__(self, config, layer_id=0):
        super().__init__()
        self.in_conv_dim = config.conv_dim[layer_id - 1] if layer_id > 0 else 1
        self.out_conv_dim = config.conv_dim[layer_id]

        self.conv = nn.Conv1d(
            self.in_conv_dim,
            self.out_conv_dim,
            kernel_size=config.conv_kernel[layer_id],
            stride=config.conv_stride[layer_id],
            has_bias=config.conv_bias,
            pad_mode="pad",
        )
        self.activation = ACT2FN[config.feat_extract_activation]

    def construct(self, hidden_states):
        hidden_states = self.conv(hidden_states)
        hidden_states = self.activation(hidden_states)
        return hidden_states


# Copied from transformers.models.wav2vec2.modeling_wav2vec2.Wav2Vec2LayerNormConvLayer with Wav2Vec2->SEWD
class SEWDLayerNormConvLayer(nn.Cell):
    def __init__(self, config, layer_id=0):
        super().__init__()
        self.in_conv_dim = config.conv_dim[layer_id - 1] if layer_id > 0 else 1
        self.out_conv_dim = config.conv_dim[layer_id]

        self.conv = nn.Conv1d(
            self.in_conv_dim,
            self.out_conv_dim,
            kernel_size=config.conv_kernel[layer_id],
            stride=config.conv_stride[layer_id],
            has_bias=config.conv_bias,
            pad_mode="pad",
        )
        self.layer_norm = nn.LayerNorm(self.out_conv_dim)
        self.activation = ACT2FN[config.feat_extract_activation]

    def construct(self, hidden_states):
        hidden_states = self.conv(hidden_states)

        hidden_states = hidden_states.swapaxes(-2, -1)
        hidden_states = self.layer_norm(hidden_states)
        hidden_states = hidden_states.swapaxes(-2, -1)

        hidden_states = self.activation(hidden_states)
        return hidden_states


# Copied from transformers.models.wav2vec2.modeling_wav2vec2.Wav2Vec2GroupNormConvLayer with Wav2Vec2->SEWD
class SEWDGroupNormConvLayer(nn.Cell):
    def __init__(self, config, layer_id=0):
        super().__init__()
        self.in_conv_dim = config.conv_dim[layer_id - 1] if layer_id > 0 else 1
        self.out_conv_dim = config.conv_dim[layer_id]

        self.conv = nn.Conv1d(
            self.in_conv_dim,
            self.out_conv_dim,
            kernel_size=config.conv_kernel[layer_id],
            stride=config.conv_stride[layer_id],
            has_bias=config.conv_bias,
            pad_mode="pad",
        )
        self.activation = ACT2FN[config.feat_extract_activation]

        self.layer_norm = nn.GroupNorm(
            num_groups=self.out_conv_dim, num_channels=self.out_conv_dim, affine=True
        )

    def construct(self, hidden_states):
        hidden_states = self.conv(hidden_states)
        hidden_states = self.layer_norm(hidden_states)
        hidden_states = self.activation(hidden_states)
        return hidden_states


# Copied from transformers.models.sew.modeling_sew.SEWPositionalConvEmbedding with SEW->SEWD
class SEWDPositionalConvEmbedding(nn.Cell):
    def __init__(self, config):
        super().__init__()
        self.conv = nn.Conv1d(
            in_channels=config.hidden_size,
            out_channels=config.hidden_size,
            kernel_size=config.num_conv_pos_embeddings,
            padding=config.num_conv_pos_embeddings // 2,
            group=config.num_conv_pos_embedding_groups,
            stride=config.squeeze_factor,
            pad_mode="pad",
            has_bias=True,
        )
        self.conv = weight_norm(self.conv, dim=2)

        self.padding = SEWDSamePadLayer(config.num_conv_pos_embeddings)
        self.activation = ACT2FN[config.feat_extract_activation]

    def construct(self, hidden_states):
        hidden_states = self.conv(hidden_states)
        hidden_states = self.padding(hidden_states)
        hidden_states = self.activation(hidden_states)

        return hidden_states


# Copied from transformers.models.wav2vec2.modeling_wav2vec2.Wav2Vec2SamePadLayer with Wav2Vec2->SEW
class SEWDSamePadLayer(nn.Cell):
    def __init__(self, num_conv_pos_embeddings):
        super().__init__()
        self.num_pad_remove = 1 if num_conv_pos_embeddings % 2 == 0 else 0

    def construct(self, hidden_states):
        if self.num_pad_remove > 0:
            hidden_states = hidden_states[:, :, : -self.num_pad_remove]
        return hidden_states


# Copied from transformers.models.sew.modeling_sew.SEWUpsampling with SEW->SEWD
class SEWDUpsampling(nn.Cell):
    def __init__(self, config):
        super().__init__()
        self.projection = nn.Dense(
            config.hidden_size, config.hidden_size * config.squeeze_factor
        )
        self.activation = ACT2FN[config.feat_extract_activation]
        self.squeeze_factor = config.squeeze_factor

    def construct(self, hidden_states):
        hidden_states = self.projection(hidden_states)
        hidden_states = self.activation(hidden_states)

        if self.squeeze_factor > 1:
            # transform embedding channels to sequence length
            bsz, src_len, src_embed_dim = hidden_states.shape
            tgt_len = src_len * self.squeeze_factor
            tgt_embed_dim = src_embed_dim // self.squeeze_factor
            hidden_states = hidden_states.reshape(
                bsz, src_len, self.squeeze_factor, tgt_embed_dim
            )
            hidden_states = hidden_states.reshape(bsz, tgt_len, tgt_embed_dim)

        return hidden_states


# Copied from transformers.models.wav2vec2.modeling_wav2vec2.Wav2Vec2FeatureEncoder with Wav2Vec2->SEWD
class SEWDFeatureEncoder(nn.Cell):
    """Construct the features from raw audio waveform"""

    def __init__(self, config):
        super().__init__()

        if config.feat_extract_norm == "group":
            conv_layers = [SEWDGroupNormConvLayer(config, layer_id=0)] + [
                SEWDNoLayerNormConvLayer(config, layer_id=i + 1)
                for i in range(config.num_feat_extract_layers - 1)
            ]
        elif config.feat_extract_norm == "layer":
            conv_layers = [
                SEWDLayerNormConvLayer(config, layer_id=i)
                for i in range(config.num_feat_extract_layers)
            ]
        else:
            raise ValueError(
                f"`config.feat_extract_norm` is {config.feat_extract_norm}, but has to be one of ['group', 'layer']"
            )
        self.conv_layers = nn.CellList(conv_layers)
        self.gradient_checkpointing = False
        self._requires_grad = True

    def _freeze_parameters(self):
        for name, param in self.parameters_and_names():
            param.requires_grad = False
        self._requires_grad = False

    def construct(self, input_values):
        hidden_states = input_values[:, None]

        # make sure hidden_states require grad for gradient_checkpointing
        if self._requires_grad and self.training:
            hidden_states.requires_grad = True

        for conv_layer in self.conv_layers:
            if self._requires_grad and self.gradient_checkpointing and self.training:
                hidden_states = self._gradient_checkpointing_func(
                    conv_layer.__call__,
                    hidden_states,
                )
            else:
                hidden_states = conv_layer(hidden_states)

        return hidden_states


class SEWDFeatureExtractor(SEWDFeatureEncoder):
    def __init__(self, config):
        super().__init__(config)
        warnings.warn(
            f"The class `{self.__class__.__name__}` has been depreciated "
            "and will be removed in Transformers v5. "
            f"Use `{self.__class__.__bases__[0].__name__}` instead.",
            FutureWarning,
        )


# Copied from transformers.models.deberta.modeling_deberta.ContextPooler
class ContextPooler(nn.Cell):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Dense(config.pooler_hidden_size, config.pooler_hidden_size)
        self.dropout = StableDropout(config.pooler_dropout)
        self.config = config

    def construct(self, hidden_states):
        # We "pool" the model by simply taking the hidden state corresponding
        # to the first token.

        context_token = hidden_states[:, 0]
        context_token = self.dropout(context_token)
        pooled_output = self.dense(context_token)
        pooled_output = ACT2FN[self.config.pooler_hidden_act](pooled_output)
        return pooled_output

    @property
    def output_dim(self):
        return self.config.hidden_size


# Copied from transformers.models.deberta.modeling_deberta.XSoftmax with deberta->deberta_v2
class XSoftmax(nn.Cell):

    def __init__(self, dim=-1):

        super().__init__()
        self.dim = dim

    def construct(self, input, mask):
        """
        Constructs a softmax operation with masking for a given input tensor.

        Args:
            self (XSoftmax): An instance of the XSoftmax class.
            input (Tensor): The input tensor on which the softmax operation is performed.
            mask (Tensor): A tensor representing the mask used for masking certain elements in the input tensor.

        Returns:
            None. The method modifies the input tensor in-place and does not return any value.

        Raises:
            - TypeError: If the input tensor or the mask tensor is not of the expected type.
            - ValueError: If the dimensions of the input tensor and the mask tensor do not match.
            - RuntimeError: If an error occurs during the softmax operation or masking process.
        """
        rmask = ~(mask.to(mindspore.bool_))
        output = input.masked_fill(rmask, mindspore.tensor(finfo(input.dtype, "min")))
        output = ops.softmax(output, self.dim)
        output = output.masked_fill(rmask, 0)
        return output

    def brop(self, input, mask, output, grad_output):
        """
        This method, 'brop', is a member of the 'XSoftmax' class and performs a specific operation on the given input, mask, output, and grad_output parameters.

        Args:
            self: An instance of the 'XSoftmax' class.
            input: The input parameter of type <input_type>. It represents the input value used in the operation.
            mask: The mask parameter of type <mask_type>. It represents a mask used in the operation. <Additional details about the purpose and restrictions of the mask parameter.>
            output: The output parameter of type <output_type>. It represents the output value of the operation.
            grad_output: The grad_output parameter of type <grad_output_type>. It represents the gradient of the output value.

        Returns:
            dx: A value of type <dx_type>. It represents the final result of the operation. <Additional details about the purpose and format of the dx value.>
            None

        Raises:
            <Exception1>: <Description of when and why this exception may be raised.>
            <Exception2>: <Description of when and why this exception may be raised.>
            .
            .
            <Additional exceptions that may be raised during the execution of the method.>
        """
        dx = ops.mul(
            output,
            ops.sub(
                grad_output,
                ops.sum(ops.mul(output, grad_output), self.dim, keepdim=True),
            ),
        )
        return dx, None


# Copied from transformers.models.deberta.modeling_deberta.DropoutContext
class DropoutContext:
    '''
    dropout context for optimization
    '''
    def __init__(self):
        self.dropout = 0
        self.mask = None
        self.scale = 1
        self.reuse_mask = True


class XDropout(nn.Cell):
    """Optimized dropout function to save computation and memory by using mask operation instead of multiplication."""

    def __init__(self, local_ctx):
        super(XDropout, self).__init__()
        self.local_ctx = local_ctx

    def construct(self, input):
        mask, dropout = get_mask(input, self.local_ctx)
        scale = 1.0 / (1 - dropout)
        if dropout > 0:
            output = input * mask * scale
        else:
            output = input
        return output

    def bprop(self, input, output, dout):
        mask, dropout = get_mask(input, self.local_ctx)
        scale = 1.0 / (1 - dropout)
        if dropout > 0:
            grad_input = dout * mask * scale
        else:
            grad_input = dout
        return grad_input, None


# Copied from transformers.models.deberta.modeling_deberta.StableDropout
class StableDropout(nn.Cell):
    """
    Optimized dropout module for stabilizing the training

    Args:
        drop_prob (float): the dropout probabilities
    """

    def __init__(self, drop_prob):
        super().__init__()
        self.drop_prob = drop_prob
        self.count = 0
        self.context_stack = None

    def construct(self, x):
        """
        Call the module

        Args:
            x (`mindspore.Tensor`): The input tensor to apply dropout
        """
        if self.training and self.drop_prob > 0:
            return XDropout(self.get_context())(x)
        return x

    def clear_context(self):
        self.count = 0
        self.context_stack = None

    def init_context(self, reuse_mask=True, scale=1):
        if self.context_stack is None:
            self.context_stack = []
        self.count = 0
        for c in self.context_stack:
            c.reuse_mask = reuse_mask
            c.scale = scale

    def get_context(self):
        if self.context_stack is not None:
            if self.count >= len(self.context_stack):
                self.context_stack.append(DropoutContext())
            ctx = self.context_stack[self.count]
            ctx.dropout = self.drop_prob
            self.count += 1
            return ctx
        else:
            return self.drop_prob


# Copied from transformers.models.deberta.modeling_deberta.DebertaSelfOutput with DebertaV2->SEWD, DebertaLayerNorm->LayerNorm, hidden_dropout_prob->activation_dropout
class SEWDSelfOutput(nn.Cell):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Dense(config.hidden_size, config.hidden_size)
        self.LayerNorm = nn.LayerNorm(
            [config.hidden_size], epsilon=config.layer_norm_eps
        )
        self.dropout = StableDropout(config.activation_dropout)

    def construct(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


# Copied from transformers.models.deberta_v2.modeling_deberta_v2.DisentangledSelfAttention with attention_probs_dropout_prob->attention_dropout, hidden_dropout_prob->activation_dropout
class DisentangledSelfAttention(nn.Cell):
    """
    Disentangled self-attention module

    Parameters:
        config (`DebertaV2Config`):
            A model config class instance with the configuration to build a new model. The schema is similar to
            *BertConfig*, for more details, please refer [`DebertaV2Config`]

    """

    def __init__(self, config):
        super().__init__()
        if config.hidden_size % config.num_attention_heads != 0:
            raise ValueError(
                f"The hidden size ({config.hidden_size}) is not a multiple of the number of attention "
                f"heads ({config.num_attention_heads})"
            )
        self.num_attention_heads = config.num_attention_heads
        _attention_head_size = config.hidden_size // config.num_attention_heads
        self.attention_head_size = getattr(
            config, "attention_head_size", _attention_head_size
        )
        self.all_head_size = self.num_attention_heads * self.attention_head_size
        self.query_proj = nn.Dense(
            config.hidden_size, self.all_head_size, has_bias=True
        )
        self.key_proj = nn.Dense(config.hidden_size, self.all_head_size, has_bias=True)
        self.value_proj = nn.Dense(
            config.hidden_size, self.all_head_size, has_bias=True
        )

        self.share_att_key = getattr(config, "share_att_key", False)
        self.pos_att_type = (
            config.pos_att_type if config.pos_att_type is not None else []
        )
        self.relative_attention = getattr(config, "relative_attention", False)

        if self.relative_attention:
            self.position_buckets = getattr(config, "position_buckets", -1)
            self.max_relative_positions = getattr(config, "max_relative_positions", -1)
            if self.max_relative_positions < 1:
                self.max_relative_positions = config.max_position_embeddings
            self.pos_ebd_size = self.max_relative_positions
            if self.position_buckets > 0:
                self.pos_ebd_size = self.position_buckets

            self.pos_dropout = StableDropout(config.activation_dropout)

            if not self.share_att_key:
                if "c2p" in self.pos_att_type:
                    self.pos_key_proj = nn.Dense(
                        config.hidden_size, self.all_head_size, has_bias=True
                    )
                if "p2c" in self.pos_att_type:
                    self.pos_query_proj = nn.Dense(
                        config.hidden_size, self.all_head_size
                    )

        self.dropout = StableDropout(config.attention_dropout)

    def swapaxes_for_scores(self, x, attention_heads):
        new_x_shape = x.shape[:-1] + (attention_heads, -1)
        x = x.view(new_x_shape)
        return x.permute(0, 2, 1, 3).view(-1, x.shape[1], x.shape[-1])

    def construct(
        self,
        hidden_states,
        attention_mask,
        output_attentions=False,
        query_states=None,
        relative_pos=None,
        rel_embeddings=None,
    ):

        if query_states is None:
            query_states = hidden_states
        query_layer = self.swapaxes_for_scores(
            self.query_proj(query_states), self.num_attention_heads
        )
        key_layer = self.swapaxes_for_scores(
            self.key_proj(hidden_states), self.num_attention_heads
        )
        value_layer = self.swapaxes_for_scores(
            self.value_proj(hidden_states), self.num_attention_heads
        )

        rel_att = None
        # Take the dot product between "query" and "key" to get the raw attention scores.
        scale_factor = 1
        if "c2p" in self.pos_att_type:
            scale_factor += 1
        if "p2c" in self.pos_att_type:
            scale_factor += 1
        scale = ops.sqrt(
            mindspore.Tensor(query_layer.shape[-1], dtype=mindspore.float32)
            * scale_factor
        )
        attention_scores = ops.bmm(
            query_layer, key_layer.swapaxes(-1, -2) / scale.to(dtype=query_layer.dtype)
        )
        if self.relative_attention:
            rel_embeddings = self.pos_dropout(rel_embeddings)
            rel_att = self.disentangled_attention_bias(
                query_layer, key_layer, relative_pos, rel_embeddings, scale_factor
            )

        if rel_att is not None:
            attention_scores = attention_scores + rel_att
        attention_scores = attention_scores.view(
            -1,
            self.num_attention_heads,
            attention_scores.shape[-2],
            attention_scores.shape[-1],
        )

        # bsz x height x length x dimension
        xsoftmax = XSoftmax(-1)
        attention_probs = xsoftmax(attention_scores, attention_mask)
        attention_probs = self.dropout(attention_probs)
        context_layer = ops.bmm(
            attention_probs.view(
                -1, attention_probs.shape[-2], attention_probs.shape[-1]
            ),
            value_layer,
        )
        context_layer = context_layer.view(
            -1,
            self.num_attention_heads,
            context_layer.shape[-2],
            context_layer.shape[-1],
        ).permute(0, 2, 1, 3)
        new_context_layer_shape = context_layer.shape[:-2] + (-1,)
        context_layer = context_layer.view(new_context_layer_shape)
        if output_attentions:
            return (context_layer, attention_probs)
        else:
            return context_layer

    def disentangled_attention_bias(
        self, query_layer, key_layer, relative_pos, rel_embeddings, scale_factor
    ):
        if relative_pos is None:
            q = query_layer.shape[-2]
            relative_pos = build_relative_position(
                q,
                key_layer.shape[-2],
                bucket_size=self.position_buckets,
                max_position=self.max_relative_positions,
            )
        if relative_pos.dim() == 2:
            relative_pos = relative_pos.unsqueeze(0).unsqueeze(0)
        elif relative_pos.dim() == 3:
            relative_pos = relative_pos.unsqueeze(1)
        # bsz x height x query x key
        elif relative_pos.dim() != 4:
            raise ValueError(
                f"Relative position ids must be of dim 2 or 3 or 4. {relative_pos.dim()}"
            )

        att_span = self.pos_ebd_size
        relative_pos = relative_pos.long()

        rel_embeddings = rel_embeddings[0 : att_span * 2, :].unsqueeze(0)
        if self.share_att_key:
            pos_query_layer = self.swapaxes_for_scores(
                self.query_proj(rel_embeddings), self.num_attention_heads
            ).repeat(query_layer.shape[0] // self.num_attention_heads, 1, 1)
            pos_key_layer = self.swapaxes_for_scores(
                self.key_proj(rel_embeddings), self.num_attention_heads
            ).repeat(query_layer.shape[0] // self.num_attention_heads, 1, 1)
        else:
            if "c2p" in self.pos_att_type:
                pos_key_layer = self.swapaxes_for_scores(
                    self.pos_key_proj(rel_embeddings), self.num_attention_heads
                ).repeat(
                    query_layer.shape[0] // self.num_attention_heads, 1, 1
                )  # .split(self.all_head_size, dim=-1)
            if "p2c" in self.pos_att_type:
                pos_query_layer = self.swapaxes_for_scores(
                    self.pos_query_proj(rel_embeddings), self.num_attention_heads
                ).repeat(
                    query_layer.shape[0] // self.num_attention_heads, 1, 1
                )  # .split(self.all_head_size, dim=-1)

        score = 0
        # content->position
        if "c2p" in self.pos_att_type:
            scale = ops.sqrt(
                mindspore.Tensor(pos_key_layer.shape[-1], dtype=mindspore.float32)
                * scale_factor
            )
            c2p_att = ops.bmm(query_layer, pos_key_layer.swapaxes(-1, -2))
            c2p_pos = ops.clamp(relative_pos + att_span, 0, att_span * 2 - 1)
            c2p_att = ops.gather_elements(
                c2p_att,
                dim=-1,
                index=c2p_pos.squeeze(0).expand(
                    (
                        (
                            query_layer.shape[0],
                            query_layer.shape[1],
                            relative_pos.shape[-1],
                        )
                    )
                ),
            )
            score += c2p_att / scale.to(dtype=c2p_att.dtype)

        # position->content
        if "p2c" in self.pos_att_type:
            scale = ops.sqrt(
                mindspore.Tensor(pos_query_layer.shape[-1], dtype=mindspore.float32)
                * scale_factor
            )
            if key_layer.shape[-2] != query_layer.shape[-2]:
                r_pos = build_relative_position(
                    key_layer.shape[-2],
                    key_layer.shape[-2],
                    bucket_size=self.position_buckets,
                    max_position=self.max_relative_positions,
                )
                r_pos = r_pos.unsqueeze(0)
            else:
                r_pos = relative_pos

            p2c_pos = ops.clamp(-r_pos + att_span, 0, att_span * 2 - 1)
            p2c_att = ops.bmm(key_layer, pos_query_layer.swapaxes(-1, -2))
            p2c_att = ops.gather_elements(
                p2c_att,
                dim=-1,
                index=p2c_pos.squeeze(0).expand(
                    (query_layer.shape[0], key_layer.shape[-2], key_layer.shape[-2])
                ),
            ).swapaxes(-1, -2)
            score += p2c_att / scale.to(dtype=p2c_att.dtype)

        return score


# Copied from transformers.models.deberta.modeling_deberta.DebertaAttention with Deberta->SEWD
class SEWDAttention(nn.Cell):
    def __init__(self, config):
        super().__init__()
        self.self = DisentangledSelfAttention(config)
        self.output = SEWDSelfOutput(config)
        self.config = config

    def construct(
        self,
        hidden_states,
        attention_mask,
        output_attentions=False,
        query_states=None,
        relative_pos=None,
        rel_embeddings=None,
    ):
        self_output = self.self(
            hidden_states,
            attention_mask,
            output_attentions,
            query_states=query_states,
            relative_pos=relative_pos,
            rel_embeddings=rel_embeddings,
        )
        if output_attentions:
            self_output, att_matrix = self_output
        if query_states is None:
            query_states = hidden_states
        attention_output = self.output(self_output, query_states)

        if output_attentions:
            return (attention_output, att_matrix)
        else:
            return attention_output


# Copied from transformers.models.bert.modeling_bert.BertIntermediate with Bert->SEWD
class SEWDIntermediate(nn.Cell):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Dense(config.hidden_size, config.intermediate_size)
        if isinstance(config.hidden_act, str):
            self.intermediate_act_fn = ACT2FN[config.hidden_act]
        else:
            self.intermediate_act_fn = config.hidden_act

    def construct(self, hidden_states: mindspore.Tensor) -> mindspore.Tensor:
        hidden_states = self.dense(hidden_states)
        hidden_states = self.intermediate_act_fn(hidden_states)
        return hidden_states


# Copied from transformers.models.deberta.modeling_deberta.DebertaOutput with DebertaLayerNorm->LayerNorm, hidden_dropout_prob->activation_dropout
class SEWDOutput(nn.Cell):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Dense(config.intermediate_size, config.hidden_size)
        self.LayerNorm = nn.LayerNorm(
            [config.hidden_size], epsilon=config.layer_norm_eps
        )
        self.dropout = StableDropout(config.activation_dropout)
        self.config = config

    def construct(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


# Copied from transformers.models.deberta.modeling_deberta.DebertaLayer with Deberta->SEWD
class SEWDLayer(nn.Cell):
    def __init__(self, config):
        super().__init__()
        self.attention = SEWDAttention(config)
        self.intermediate = SEWDIntermediate(config)
        self.output = SEWDOutput(config)

    def construct(
        self,
        hidden_states,
        attention_mask,
        query_states=None,
        relative_pos=None,
        rel_embeddings=None,
        output_attentions=False,
    ):
        attention_output = self.attention(
            hidden_states,
            attention_mask,
            output_attentions=output_attentions,
            query_states=query_states,
            relative_pos=relative_pos,
            rel_embeddings=rel_embeddings,
        )
        if output_attentions:
            attention_output, att_matrix = attention_output
        intermediate_output = self.intermediate(attention_output)
        layer_output = self.output(intermediate_output, attention_output)
        if output_attentions:
            return (layer_output, att_matrix)
        else:
            return layer_output


# Copied from transformers.models.deberta_v2.modeling_deberta_v2.ConvLayer
class ConvLayer(nn.Cell):
    def __init__(self, config):
        super().__init__()
        kernel_size = getattr(config, "conv_kernel_size", 3)
        groups = getattr(config, "conv_groups", 1)
        self.conv_act = getattr(config, "conv_act", "tanh")
        self.conv = nn.Conv1d(
            config.hidden_size,
            config.hidden_size,
            kernel_size,
            padding=(kernel_size - 1) // 2,
            group=groups,
            pad_mode="pad",
            has_bias=True,
        )
        self.LayerNorm = nn.LayerNorm(
            [config.hidden_size], epsilon=config.layer_norm_eps
        )
        self.dropout = StableDropout(config.hidden_dropout_prob)
        self.config = config

    def construct(self, hidden_states, residual_states, input_mask):
        out = self.conv(hidden_states.permute(0, 2, 1)).permute(0, 2, 1)
        rmask = (1 - input_mask).bool()
        out.masked_fill_(rmask.unsqueeze(-1).expand((out.shape)), 0)
        out = ACT2FN[self.conv_act](self.dropout(out))

        layer_norm_input = residual_states + out
        output = self.LayerNorm(layer_norm_input).to(layer_norm_input)

        if input_mask is None:
            output_states = output
        else:
            if input_mask.dim() != layer_norm_input.dim():
                if input_mask.dim() == 4:
                    input_mask = input_mask.squeeze(1).squeeze(1)
                input_mask = input_mask.unsqueeze(2)

            input_mask = input_mask.to(output.dtype)
            output_states = output * input_mask

        return output_states


# Copied from transformers.models.deberta_v2.modeling_deberta_v2.DebertaV2Encoder with DebertaV2->SEWD
class SEWDTransformerEncoder(nn.Cell):
    """Modified BertEncoder with relative position bias support"""

    def __init__(self, config):
        super().__init__()

        self.layer = nn.CellList(
            [SEWDLayer(config) for _ in range(config.num_hidden_layers)]
        )
        self.relative_attention = getattr(config, "relative_attention", False)

        if self.relative_attention:
            self.max_relative_positions = getattr(config, "max_relative_positions", -1)
            if self.max_relative_positions < 1:
                self.max_relative_positions = config.max_position_embeddings

            self.position_buckets = getattr(config, "position_buckets", -1)
            pos_ebd_size = self.max_relative_positions * 2

            if self.position_buckets > 0:
                pos_ebd_size = self.position_buckets * 2

            self.rel_embeddings = nn.Embedding(pos_ebd_size, config.hidden_size)

        self.norm_rel_ebd = [
            x.strip()
            for x in getattr(config, "norm_rel_ebd", "none").lower().split("|")
        ]

        if "layer_norm" in self.norm_rel_ebd:
            self.LayerNorm = nn.LayerNorm(
                [config.hidden_size], epsilon=config.layer_norm_eps
            )

        self.conv = (
            ConvLayer(config) if getattr(config, "conv_kernel_size", 0) > 0 else None
        )
        self.gradient_checkpointing = False

    def get_rel_embedding(self):
        rel_embeddings = self.rel_embeddings.weight if self.relative_attention else None
        if rel_embeddings is not None and ("layer_norm" in self.norm_rel_ebd):
            rel_embeddings = self.LayerNorm(rel_embeddings)
        return rel_embeddings

    def get_attention_mask(self, attention_mask):
        if attention_mask.dim() <= 2:
            extended_attention_mask = attention_mask.unsqueeze(1).unsqueeze(2)
            attention_mask = extended_attention_mask * extended_attention_mask.squeeze(
                -2
            ).unsqueeze(-1)
        elif attention_mask.dim() == 3:
            attention_mask = attention_mask.unsqueeze(1)

        return attention_mask

    def get_rel_pos(self, hidden_states, query_states=None, relative_pos=None):
        if self.relative_attention and relative_pos is None:
            q = (
                query_states.shape[-2]
                if query_states is not None
                else hidden_states.shape[-2]
            )
            relative_pos = build_relative_position(
                q,
                hidden_states.shape[-2],
                bucket_size=self.position_buckets,
                max_position=self.max_relative_positions,
            )
        return relative_pos

    def construct(
        self,
        hidden_states,
        attention_mask,
        output_hidden_states=True,
        output_attentions=False,
        query_states=None,
        relative_pos=None,
        return_dict=True,
    ):
        if attention_mask.dim() <= 2:
            input_mask = attention_mask
        else:
            input_mask = attention_mask.sum(-2) > 0
        attention_mask = self.get_attention_mask(attention_mask)
        relative_pos = self.get_rel_pos(hidden_states, query_states, relative_pos)

        all_hidden_states = () if output_hidden_states else None
        all_attentions = () if output_attentions else None

        if isinstance(hidden_states, Sequence):
            next_kv = hidden_states[0]
        else:
            next_kv = hidden_states
        rel_embeddings = self.get_rel_embedding()
        output_states = next_kv
        for i, layer_module in enumerate(self.layer):
            if output_hidden_states:
                all_hidden_states = all_hidden_states + (output_states,)

            if self.gradient_checkpointing and self.training:
                output_states = self._gradient_checkpointing_func(
                    layer_module.__call__,
                    next_kv,
                    attention_mask,
                    query_states,
                    relative_pos,
                    rel_embeddings,
                    output_attentions,
                )
            else:
                output_states = layer_module(
                    next_kv,
                    attention_mask,
                    query_states=query_states,
                    relative_pos=relative_pos,
                    rel_embeddings=rel_embeddings,
                    output_attentions=output_attentions,
                )

            if output_attentions:
                output_states, att_m = output_states

            if i == 0 and self.conv is not None:
                output_states = self.conv(hidden_states, output_states, input_mask)

            if query_states is not None:
                query_states = output_states
                if isinstance(hidden_states, Sequence):
                    next_kv = hidden_states[i + 1] if i + 1 < len(self.layer) else None
            else:
                next_kv = output_states

            if output_attentions:
                all_attentions = all_attentions + (att_m,)

        if output_hidden_states:
            all_hidden_states = all_hidden_states + (output_states,)

        if not return_dict:
            return tuple(
                v
                for v in [output_states, all_hidden_states, all_attentions]
                if v is not None
            )
        return BaseModelOutput(
            last_hidden_state=output_states,
            hidden_states=all_hidden_states,
            attentions=all_attentions,
        )


class SEWDEncoder(nn.Cell):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.pos_conv_embed = SEWDPositionalConvEmbedding(config)
        self.pool = nn.AvgPool1d(config.squeeze_factor, config.squeeze_factor)
        self.encoder = SEWDTransformerEncoder(config)
        self.upsample = SEWDUpsampling(config)
        self.gradient_checkpointing = False

    def construct(
        self,
        hidden_states: mindspore.Tensor,
        attention_mask: Optional[mindspore.Tensor] = None,
        output_attentions: bool = False,
        output_hidden_states: bool = False,
        return_dict: bool = True,
    ):
        max_encoder_length = hidden_states.shape[1] // self.config.squeeze_factor
        if attention_mask is None:
            attention_mask = ops.ones(
                (hidden_states.shape[0], max_encoder_length), dtype=mindspore.int64
            )
        else:
            # make sure padded tokens output 0
            hidden_states[~attention_mask.bool()] = 0.0

            input_lengths = (attention_mask.long()).sum(-1)
            # apply pooling formula to get real output_lengths
            output_lengths = input_lengths // self.config.squeeze_factor
            attention_ids = (
                ops.arange(0, max_encoder_length)
                .view(1, -1)
                .expand((output_lengths.shape[0], -1))
            )
            attention_mask = (attention_ids < output_lengths.view(-1, 1)).long()

        n_input_timesteps = hidden_states.shape[1]

        hidden_states = hidden_states.swapaxes(1, 2)
        position_embeddings = self.pos_conv_embed(hidden_states)
        pooled_hidden_states = self.pool(hidden_states)
        min_length = min(position_embeddings.shape[-1], pooled_hidden_states.shape[-1])
        hidden_states = (
            pooled_hidden_states[..., :min_length]
            + position_embeddings[..., :min_length]
        )
        hidden_states = hidden_states.swapaxes(1, 2)

        encoder_outputs = self.encoder(
            hidden_states, attention_mask, output_hidden_states, output_attentions
        )

        hidden_states = self.upsample(encoder_outputs.last_hidden_state)
        if hidden_states.shape[1] < n_input_timesteps:
            hidden_states = ops.pad(
                hidden_states, (0, 0, 0, n_input_timesteps - hidden_states.shape[1])
            )

        if not return_dict:
            return tuple(
                v
                for v in [
                    hidden_states,
                    encoder_outputs.hidden_states,
                    encoder_outputs.attentions,
                ]
                if v is not None
            )
        return BaseModelOutput(
            last_hidden_state=hidden_states,
            hidden_states=encoder_outputs.hidden_states,
            attentions=encoder_outputs.attentions,
        )


class SEWDPreTrainedModel(PreTrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models.
    """

    config_class = SEWDConfig
    base_model_prefix = "sew-d"
    main_input_name = "input_values"
    supports_gradient_checkpointing = True

    def _init_weights(self, cell):
        """Initialize the weights."""
        if isinstance(cell, SEWDPositionalConvEmbedding):
            # 使用正态分布初始化权重
            cell.conv.weight.set_data(
                initializer(
                    Normal(
                        mean=0,
                        sigma=2
                        * math.sqrt(
                            1 / (cell.conv.kernel_size[0] * cell.conv.in_channels)
                        ),
                    ),
                    cell.conv.weight.shape,
                    cell.conv.weight.dtype,
                )
            )
            cell.conv.bias.set_data(
                initializer("zeros", cell.conv.bias.shape, cell.conv.bias.dtype)
            )
        elif isinstance(cell, nn.Dense):
            # 使用正态分布初始化权重
            cell.weight.set_data(
                initializer(
                    Normal(0.0, self.config.initializer_range),
                    cell.weight.shape,
                    cell.weight.dtype,
                )
            )
        elif isinstance(cell, (nn.LayerNorm, nn.GroupNorm)):
            cell.bias.set_data(initializer("zeros", cell.bias.shape, cell.bias.dtype))
            cell.weight.set_data(
                initializer("ones", cell.weight.shape, cell.weight.dtype)
            )
        elif isinstance(cell, nn.Conv1d):
            # 使用kaiming正态分布初始化权重
            cell.weight.set_data(
                initializer("he_normal", cell.weight.shape, cell.weight.dtype)
            )
        elif isinstance(cell, nn.Embedding):
            weight = np.random.normal(
                0.0, self.config.initializer_range, cell.weight.shape
            )
            if cell.padding_idx:
                weight[cell.padding_idx] = 0.0

            cell.weight.set_data(mindspore.Tensor(weight, cell.weight.dtype))

        if isinstance(cell, (nn.Dense, nn.Conv1d)) and cell.bias is not None:
            cell.bias.set_data(initializer("zeros", cell.bias.shape, cell.bias.dtype))

    def _get_feat_extract_output_lengths(
        self, input_lengths: Union[mindspore.Tensor, int]
    ):
        """
        Computes the output length of the convolutional layers
        """

        def _conv_out_length(input_length, kernel_size, stride):
            # 1D convolutional layer output length formula taken
            return (
                ops.div(input_length - kernel_size, stride, rounding_mode="floor") + 1
            )

        for kernel_size, stride in zip(
            self.config.conv_kernel, self.config.conv_stride
        ):
            input_lengths = _conv_out_length(input_lengths, kernel_size, stride)

        return input_lengths

    def _get_feature_vector_attention_mask(
        self, feature_vector_length: int, attention_mask: mindspore.Tensor
    ):
        output_lengths = self._get_feat_extract_output_lengths(
            attention_mask.sum(-1)
        ).to(mindspore.int64)
        batch_size = attention_mask.shape[0]

        attention_mask = ops.zeros(
            (batch_size, feature_vector_length), dtype=attention_mask.dtype
        )
        # these two operations makes sure that all values before the output lengths idxs are attended to
        attention_mask[(ops.arange(attention_mask.shape[0]), output_lengths - 1)] = 1
        attention_mask = attention_mask.flip([-1]).cumsum(-1).flip([-1]).bool()
        return attention_mask


# Copied from transformers.models.sew.modeling_sew.SEWModel with SEW->SEWD, layer_norm_eps->feature_layer_norm_eps
class SEWDModel(SEWDPreTrainedModel):
    def __init__(self, config: SEWDConfig):
        super().__init__(config)
        self.config = config
        self.feature_extractor = SEWDFeatureEncoder(config)
        self.layer_norm = nn.LayerNorm(
            [config.conv_dim[-1]], epsilon=config.feature_layer_norm_eps
        )

        self.project_features = config.conv_dim[-1] != config.hidden_size
        if self.project_features:
            self.feature_projection = nn.Dense(config.conv_dim[-1], config.hidden_size)
        self.feature_dropout = nn.Dropout(config.feat_proj_dropout)

        if config.mask_time_prob > 0.0 or config.mask_feature_prob > 0.0:
            self.masked_spec_embed = mindspore.Parameter(
                ops.uniform(
                    shape=mindspore.Tensor(config.hidden_size),
                    minval=mindspore.Tensor(0.0),
                    maxval=mindspore.Tensor(1.0),
                )
            )

        self.encoder = SEWDEncoder(config)

        # Initialize weights and apply final processing
        self.post_init()

    # Copied from transformers.models.wav2vec2.modeling_wav2vec2.Wav2Vec2Model._mask_hidden_states
    def _mask_hidden_states(
        self,
        hidden_states: mindspore.Tensor,
        mask_time_indices: Optional[mindspore.Tensor] = None,
        attention_mask: Optional[mindspore.Tensor] = None,
    ):
        """
        Masks extracted features along time axis and/or along feature axis according to
        [SpecAugment](https://arxiv.org/abs/1904.08779).
        """

        # `config.apply_spec_augment` can set masking to False
        if not getattr(self.config, "apply_spec_augment", True):
            return hidden_states

        # generate indices & apply SpecAugment along time axis
        batch_size, sequence_length, hidden_size = hidden_states.shape

        if mask_time_indices is not None:
            # apply SpecAugment along time axis with given mask_time_indices
            hidden_states[mask_time_indices] = self.masked_spec_embed.to(
                hidden_states.dtype
            )
        elif self.config.mask_time_prob > 0 and self.training:
            mask_time_indices = _compute_mask_indices(
                (batch_size, sequence_length),
                mask_prob=self.config.mask_time_prob,
                mask_length=self.config.mask_time_length,
                attention_mask=attention_mask,
                min_masks=self.config.mask_time_min_masks,
            )
            mask_time_indices = mindspore.Tensor(
                mask_time_indices, dtype=mindspore.bool_
            )
            hidden_states[mask_time_indices] = self.masked_spec_embed.to(
                hidden_states.dtype
            )

        if self.config.mask_feature_prob > 0 and self.training:
            # generate indices & apply SpecAugment along feature axis
            mask_feature_indices = _compute_mask_indices(
                (batch_size, hidden_size),
                mask_prob=self.config.mask_feature_prob,
                mask_length=self.config.mask_feature_length,
                min_masks=self.config.mask_feature_min_masks,
            )
            mask_feature_indices = mindspore.Tensor(
                mask_feature_indices, dtype=mindspore.bool_
            )
            mask_feature_indices = mask_feature_indices[:, None].expand(
                (-1, sequence_length, -1)
            )
            hidden_states[mask_feature_indices] = 0

        return hidden_states

    def construct(
        self,
        input_values: Optional[mindspore.Tensor],
        attention_mask: Optional[mindspore.Tensor] = None,
        mask_time_indices: Optional[mindspore.Tensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, BaseModelOutput]:
        output_attentions = (
            output_attentions
            if output_attentions is not None
            else self.config.output_attentions
        )
        output_hidden_states = (
            output_hidden_states
            if output_hidden_states is not None
            else self.config.output_hidden_states
        )
        return_dict = (
            return_dict if return_dict is not None else self.config.use_return_dict
        )

        extract_features = self.feature_extractor(input_values)
        extract_features = extract_features.swapaxes(1, 2)
        extract_features = self.layer_norm(extract_features)

        if self.project_features:
            extract_features = self.feature_projection(extract_features)
        hidden_states = self.feature_dropout(extract_features)

        if attention_mask is not None:
            # compute reduced attention_mask corresponding to feature vectors
            attention_mask = self._get_feature_vector_attention_mask(
                hidden_states.shape[1], attention_mask
            )

        hidden_states = self._mask_hidden_states(
            hidden_states, mask_time_indices=mask_time_indices
        )

        encoder_outputs = self.encoder(
            hidden_states,
            attention_mask=attention_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        hidden_states = encoder_outputs[0]

        if not return_dict:
            return (hidden_states,) + encoder_outputs[1:]

        return BaseModelOutput(
            last_hidden_state=hidden_states,
            hidden_states=encoder_outputs.hidden_states,
            attentions=encoder_outputs.attentions,
        )


# Copied from transformers.models.wav2vec2.modeling_wav2vec2.Wav2Vec2ForCTC with Wav2Vec2->SEWD, wav2vec2->sew_d, WAV_2_VEC_2->SEWD
class SEWDForCTC(SEWDPreTrainedModel):
    def __init__(self, config, target_lang: Optional[str] = None):
        super().__init__(config)

        self.sew_d = SEWDModel(config)
        self.dropout = nn.Dropout(config.final_dropout)

        self.target_lang = target_lang

        if config.vocab_size is None:
            raise ValueError(
                f"You are trying to instantiate {self.__class__} with a configuration that "
                "does not define the vocabulary size of the language model head. Please "
                "instantiate the model as follows: `SEWDForCTC.from_pretrained(..., vocab_size=vocab_size)`. "
                "or define `vocab_size` of your model's configuration."
            )
        output_hidden_size = (
            config.output_hidden_size
            if hasattr(config, "add_adapter") and config.add_adapter
            else config.hidden_size
        )
        self.lm_head = nn.Dense(output_hidden_size, config.vocab_size)

        # Initialize weights and apply final processing
        self.post_init()

    def tie_weights(self):
        """
        This method overwrites [`~PreTrainedModel.tie_weights`] so that adapter weights can be correctly loaded when
        passing `target_lang=...` to `from_pretrained(...)`.

        This method is **not** supposed to be called by the user and is prone to be changed in the future.
        """

        # Note that `tie_weights` is usually used to tie input and output embedding weights. The method is re-purposed to
        # correctly load adapter layers for SEWD so that we do not have to introduce a new API to
        # [`PreTrainedModel`]. While slightly hacky, SEWD never has to tie input and output embeddings, so that it is
        # ok to repurpose this function here.
        target_lang = self.target_lang

        if (
            target_lang is not None
            and getattr(self.config, "adapter_attn_dim", None) is None
        ):
            raise ValueError(
                f"Cannot pass `target_lang`: {target_lang} if `config.adapter_attn_dim` is not defined."
            )
        elif (
            target_lang is None
            and getattr(self.config, "adapter_attn_dim", None) is not None
        ):
            logger.info("By default `target_lang` is set to 'eng'.")
        elif target_lang is not None:
            self.load_adapter(target_lang)

    def freeze_feature_extractor(self):
        """
        Calling this function will disable the gradient computation for the feature encoder so that its parameter will
        not be updated during training.
        """
        warnings.warn(
            "The method `freeze_feature_extractor` is deprecated and will be removed in Transformers v5. "
            "Please use the equivalent `freeze_feature_encoder` method instead.",
            FutureWarning,
        )
        self.freeze_feature_encoder()

    def freeze_feature_encoder(self):
        """
        Calling this function will disable the gradient computation for the feature encoder so that its parameter will
        not be updated during training.
        """
        self.sew_d.feature_extractor._freeze_parameters()

    def freeze_base_model(self):
        """
        Calling this function will disable the gradient computation for the base model so that its parameters will not
        be updated during training. Only the classification head will be updated.
        """
        for name, param in self.sew_d.parameters_and_names():
            param.requires_grad = False

    def construct(
        self,
        input_values: Optional[mindspore.Tensor],
        attention_mask: Optional[mindspore.Tensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        labels: Optional[mindspore.Tensor] = None,
    ) -> Union[Tuple, CausalLMOutput]:
        r"""
        labels (`mindspore.Tensor` of shape `(batch_size, target_length)`, *optional*):
            Labels for connectionist temporal classification. Note that `target_length` has to be smaller or equal to
            the sequence length of the output logits. Indices are selected in `[-100, 0, ..., config.vocab_size - 1]`.
            All labels set to `-100` are ignored (masked), the loss is only computed for labels in `[0, ...,
            config.vocab_size - 1]`.
        """

        return_dict = (
            return_dict if return_dict is not None else self.config.use_return_dict
        )

        outputs = self.sew_d(
            input_values,
            attention_mask=attention_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        hidden_states = outputs[0]
        hidden_states = self.dropout(hidden_states)

        logits = self.lm_head(hidden_states)

        loss = None
        if labels is not None:
            if labels.max() >= self.config.vocab_size:
                raise ValueError(
                    f"Label values must be <= vocab_size: {self.config.vocab_size}"
                )

            # retrieve loss input_lengths from attention_mask
            attention_mask = (
                attention_mask
                if attention_mask is not None
                else ops.ones_like(input_values, dtype=mindspore.int64)
            )
            input_lengths = self._get_feat_extract_output_lengths(
                attention_mask.sum(-1)
            ).to(mindspore.int64)

            # assuming that padded tokens are filled with -100
            # when not being attended to
            labels_mask = labels >= 0
            target_lengths = labels_mask.sum(-1)

            # ctc_loss doesn't support fp16
            log_probs = ops.log_softmax(logits, axis=-1).swapaxes(0, 1)

            loss = ops.ctc_loss(
                log_probs,
                labels,
                input_lengths,
                target_lengths,
                blank=self.config.pad_token_id,
                reduction=self.config.ctc_loss_reduction,
                zero_infinity=self.config.ctc_zero_infinity,
            )[0]

        if not return_dict:
            output = (logits,) + outputs[_HIDDEN_STATES_START_POSITION:]
            return ((loss,) + output) if loss is not None else output

        return CausalLMOutput(
            loss=loss,
            logits=logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )


# Copied from transformers.models.wav2vec2.modeling_wav2vec2.Wav2Vec2ForSequenceClassification with Wav2Vec2->SEWD, wav2vec2->sew_d, WAV_2_VEC_2->SEWD
class SEWDForSequenceClassification(SEWDPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)

        if hasattr(config, "add_adapter") and config.add_adapter:
            raise ValueError(
                "Sequence classification does not support the use of SEWD adapters (config.add_adapter=True)"
            )
        self.sew_d = SEWDModel(config)
        num_layers = (
            config.num_hidden_layers + 1
        )  # transformer layers + input embeddings
        if config.use_weighted_layer_sum:
            self.layer_weights = mindspore.Parameter(ops.ones(num_layers) / num_layers)
        self.projector = nn.Dense(config.hidden_size, config.classifier_proj_size)
        self.classifier = nn.Dense(config.classifier_proj_size, config.num_labels)

        # Initialize weights and apply final processing
        self.post_init()

    def freeze_feature_extractor(self):
        """
        Calling this function will disable the gradient computation for the feature encoder so that its parameters will
        not be updated during training.
        """
        warnings.warn(
            "The method `freeze_feature_extractor` is deprecated and will be removed in Transformers v5. "
            "Please use the equivalent `freeze_feature_encoder` method instead.",
            FutureWarning,
        )
        self.freeze_feature_encoder()

    def freeze_feature_encoder(self):
        """
        Calling this function will disable the gradient computation for the feature encoder so that its parameter will
        not be updated during training.
        """
        self.sew_d.feature_extractor._freeze_parameters()

    def freeze_base_model(self):
        """
        Calling this function will disable the gradient computation for the base model so that its parameters will not
        be updated during training. Only the classification head will be updated.
        """
        for name, param in self.sew_d.parameters_and_names():
            param.requires_grad = False

    def construct(
        self,
        input_values: Optional[mindspore.Tensor],
        attention_mask: Optional[mindspore.Tensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        labels: Optional[mindspore.Tensor] = None,
    ) -> Union[Tuple, SequenceClassifierOutput]:
        r"""
        labels (`mindspore.Tensor` of shape `(batch_size,)`, *optional*):
            Labels for computing the sequence classification/regression loss. Indices should be in `[0, ...,
            config.num_labels - 1]`. If `config.num_labels == 1` a regression loss is computed (Mean-Square loss), If
            `config.num_labels > 1` a classification loss is computed (Cross-Entropy).
        """

        return_dict = (
            return_dict if return_dict is not None else self.config.use_return_dict
        )
        output_hidden_states = (
            True if self.config.use_weighted_layer_sum else output_hidden_states
        )

        outputs = self.sew_d(
            input_values,
            attention_mask=attention_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        if self.config.use_weighted_layer_sum:
            hidden_states = outputs[_HIDDEN_STATES_START_POSITION]
            hidden_states = ops.stack(hidden_states, axis=1)
            norm_weights = ops.softmax(self.layer_weights, axis=-1)
            hidden_states = (hidden_states * norm_weights.view(-1, 1, 1)).sum(axis=1)
        else:
            hidden_states = outputs[0]

        hidden_states = self.projector(hidden_states)
        if attention_mask is None:
            pooled_output = hidden_states.mean(axis=1)
        else:
            padding_mask = self._get_feature_vector_attention_mask(
                hidden_states.shape[1], attention_mask
            )
            hidden_states[~padding_mask] = 0.0
            pooled_output = hidden_states.sum(axis=1) / padding_mask.sum(axis=1).view(
                -1, 1
            )

        logits = self.classifier(pooled_output)

        loss = None
        if labels is not None:
            loss = ops.cross_entropy(
                logits.view(-1, self.config.num_labels), labels.view(-1)
            )

        if not return_dict:
            output = (logits,) + outputs[_HIDDEN_STATES_START_POSITION:]
            return ((loss,) + output) if loss is not None else output

        return SequenceClassifierOutput(
            loss=loss,
            logits=logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )


__all__ = [
    "SEWDNoLayerNormConvLayer",
    "SEWDLayerNormConvLayer",
    "SEWDGroupNormConvLayer",
    "SEWDPositionalConvEmbedding",
    "SEWDSamePadLayer",
    "SEWDUpsampling",
    "SEWDFeatureEncoder",
    "SEWDFeatureExtractor",
    "ContextPooler",
    "XSoftmax",
    "DropoutContext",
    "XDropout",
    "StableDropout",
    "SEWDSelfOutput",
    "DisentangledSelfAttention",
    "SEWDAttention",
    "SEWDIntermediate",
    "SEWDOutput",
    "SEWDLayer",
    "ConvLayer",
    "SEWDTransformerEncoder",
    "SEWDEncoder",
    "SEWDPreTrainedModel",
    "SEWDModel",
    "SEWDForCTC",
    "SEWDForSequenceClassification",
]
