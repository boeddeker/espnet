# Copyright 2021 Tomoki Hayashi
# Copyright 2022 Yifeng Yu
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

"""Generator module in VITS.

This code is based on https://github.com/jaywalnut310/vits.

"""

import math
from typing import List, Optional, Tuple

import numpy as np
import torch
import torch.nn.functional as F

from espnet2.gan_tts.hifigan import HiFiGANGenerator
from espnet2.gan_tts.utils import get_random_segments
from espnet2.gan_svs.vits.duration_predictor import (
    DurationPredictor,
    StochasticDurationPredictor,
)
from espnet2.gan_tts.vits.posterior_encoder import PosteriorEncoder
from espnet2.gan_tts.vits.residual_coupling import ResidualAffineCouplingBlock
from espnet2.gan_svs.vits.text_encoder import TextEncoder
from espnet2.gan_svs.vits.length_regulator import LengthRegulator
from espnet.nets.pytorch_backend.nets_utils import make_non_pad_mask
from espnet2.gan_svs.vits.modules import Projection, sequence_mask
from espnet.nets.pytorch_backend.transformer.embedding import (
    PositionalEncoding,
    ScaledPositionalEncoding,
)


class VITSGenerator(torch.nn.Module):
    """Generator module in VITS.

    This is a module of VITS described in `Conditional Variational Autoencoder
    with Adversarial Learning for End-to-End Text-to-Speech`_.

    As text encoder, we use conformer architecture instead of the relative positional
    Transformer, which contains additional convolution layers.

    .. _`Conditional Variational Autoencoder with Adversarial Learning for End-to-End
        Text-to-Speech`: https://arxiv.org/abs/2006.04558

    """

    def __init__(
        self,
        vocabs: int,
        midi_dim: int = 129,
        tempo_dim: int = 128,
        beat_dim: int = 128,
        midi_embed_integration_type: str = "add",
        aux_channels: int = 513,
        hidden_channels: int = 192,
        spks: Optional[int] = None,
        langs: Optional[int] = None,
        spk_embed_dim: Optional[int] = None,
        global_channels: int = -1,
        segment_size: int = 32,
        text_encoder_attention_heads: int = 2,
        text_encoder_ffn_expand: int = 4,
        text_encoder_blocks: int = 6,
        text_encoder_positionwise_layer_type: str = "conv1d",
        text_encoder_positionwise_conv_kernel_size: int = 1,
        text_encoder_positional_encoding_layer_type: str = "rel_pos",
        text_encoder_self_attention_layer_type: str = "rel_selfattn",
        text_encoder_activation_type: str = "swish",
        text_encoder_normalize_before: bool = True,
        text_encoder_dropout_rate: float = 0.1,
        text_encoder_positional_dropout_rate: float = 0.0,
        text_encoder_attention_dropout_rate: float = 0.0,
        text_encoder_conformer_kernel_size: int = 7,
        use_macaron_style_in_text_encoder: bool = True,
        use_conformer_conv_in_text_encoder: bool = True,
        decoder_kernel_size: int = 7,
        decoder_channels: int = 512,
        decoder_upsample_scales: List[int] = [8, 8, 2, 2],
        decoder_upsample_kernel_sizes: List[int] = [16, 16, 4, 4],
        decoder_resblock_kernel_sizes: List[int] = [3, 7, 11],
        decoder_resblock_dilations: List[List[int]] = [[1, 3, 5], [1, 3, 5], [1, 3, 5]],
        use_weight_norm_in_decoder: bool = True,
        posterior_encoder_kernel_size: int = 5,
        posterior_encoder_layers: int = 16,
        posterior_encoder_stacks: int = 1,
        posterior_encoder_base_dilation: int = 1,
        posterior_encoder_dropout_rate: float = 0.0,
        use_weight_norm_in_posterior_encoder: bool = True,
        flow_flows: int = 4,
        flow_kernel_size: int = 5,
        flow_base_dilation: int = 1,
        flow_layers: int = 4,
        flow_dropout_rate: float = 0.0,
        use_weight_norm_in_flow: bool = True,
        use_only_mean_in_flow: bool = True,
        stochastic_duration_predictor_kernel_size: int = 3,
        stochastic_duration_predictor_dropout_rate: float = 0.5,
        stochastic_duration_predictor_flows: int = 4,
        stochastic_duration_predictor_dds_conv_layers: int = 3,
        use_sdp: bool = False,
    ):
        """Initialize VITS generator module.

        Args:
            vocabs (int): Input vocabulary size.
            aux_channels (int): Number of acoustic feature channels.
            hidden_channels (int): Number of hidden channels.
            spks (Optional[int]): Number of speakers. If set to > 1, assume that the
                sids will be provided as the input and use sid embedding layer.
            langs (Optional[int]): Number of languages. If set to > 1, assume that the
                lids will be provided as the input and use sid embedding layer.
            spk_embed_dim (Optional[int]): Speaker embedding dimension. If set to > 0,
                assume that spembs will be provided as the input.
            global_channels (int): Number of global conditioning channels.
            segment_size (int): Segment size for decoder.
            text_encoder_attention_heads (int): Number of heads in conformer block
                of text encoder.
            text_encoder_ffn_expand (int): Expansion ratio of FFN in conformer block
                of text encoder.
            text_encoder_blocks (int): Number of conformer blocks in text encoder.
            text_encoder_positionwise_layer_type (str): Position-wise layer type in
                conformer block of text encoder.
            text_encoder_positionwise_conv_kernel_size (int): Position-wise convolution
                kernel size in conformer block of text encoder. Only used when the
                above layer type is conv1d or conv1d-linear.
            text_encoder_positional_encoding_layer_type (str): Positional encoding layer
                type in conformer block of text encoder.
            text_encoder_self_attention_layer_type (str): Self-attention layer type in
                conformer block of text encoder.
            text_encoder_activation_type (str): Activation function type in conformer
                block of text encoder.
            text_encoder_normalize_before (bool): Whether to apply layer norm before
                self-attention in conformer block of text encoder.
            text_encoder_dropout_rate (float): Dropout rate in conformer block of
                text encoder.
            text_encoder_positional_dropout_rate (float): Dropout rate for positional
                encoding in conformer block of text encoder.
            text_encoder_attention_dropout_rate (float): Dropout rate for attention in
                conformer block of text encoder.
            text_encoder_conformer_kernel_size (int): Conformer conv kernel size. It
                will be used when only use_conformer_conv_in_text_encoder = True.
            use_macaron_style_in_text_encoder (bool): Whether to use macaron style FFN
                in conformer block of text encoder.
            use_conformer_conv_in_text_encoder (bool): Whether to use covolution in
                conformer block of text encoder.
            decoder_kernel_size (int): Decoder kernel size.
            decoder_channels (int): Number of decoder initial channels.
            decoder_upsample_scales (List[int]): List of upsampling scales in decoder.
            decoder_upsample_kernel_sizes (List[int]): List of kernel size for
                upsampling layers in decoder.
            decoder_resblock_kernel_sizes (List[int]): List of kernel size for resblocks
                in decoder.
            decoder_resblock_dilations (List[List[int]]): List of list of dilations for
                resblocks in decoder.
            use_weight_norm_in_decoder (bool): Whether to apply weight normalization in
                decoder.
            posterior_encoder_kernel_size (int): Posterior encoder kernel size.
            posterior_encoder_layers (int): Number of layers of posterior encoder.
            posterior_encoder_stacks (int): Number of stacks of posterior encoder.
            posterior_encoder_base_dilation (int): Base dilation of posterior encoder.
            posterior_encoder_dropout_rate (float): Dropout rate for posterior encoder.
            use_weight_norm_in_posterior_encoder (bool): Whether to apply weight
                normalization in posterior encoder.
            flow_flows (int): Number of flows in flow.
            flow_kernel_size (int): Kernel size in flow.
            flow_base_dilation (int): Base dilation in flow.
            flow_layers (int): Number of layers in flow.
            flow_dropout_rate (float): Dropout rate in flow
            use_weight_norm_in_flow (bool): Whether to apply weight normalization in
                flow.
            use_only_mean_in_flow (bool): Whether to use only mean in flow.
            stochastic_duration_predictor_kernel_size (int): Kernel size in stochastic
                duration predictor.
            stochastic_duration_predictor_dropout_rate (float): Dropout rate in
                stochastic duration predictor.
            stochastic_duration_predictor_flows (int): Number of flows in stochastic
                duration predictor.
            stochastic_duration_predictor_dds_conv_layers (int): Number of DDS conv
                layers in stochastic duration predictor.

        """
        super().__init__()
        self.segment_size = segment_size
        self.text_encoder = TextEncoder(
            vocabs=vocabs,
            attention_dim=hidden_channels,
            attention_heads=text_encoder_attention_heads,
            linear_units=hidden_channels * text_encoder_ffn_expand,
            blocks=text_encoder_blocks,
            positionwise_layer_type=text_encoder_positionwise_layer_type,
            positionwise_conv_kernel_size=text_encoder_positionwise_conv_kernel_size,
            positional_encoding_layer_type=text_encoder_positional_encoding_layer_type,
            self_attention_layer_type=text_encoder_self_attention_layer_type,
            activation_type=text_encoder_activation_type,
            normalize_before=text_encoder_normalize_before,
            dropout_rate=text_encoder_dropout_rate,
            positional_dropout_rate=text_encoder_positional_dropout_rate,
            attention_dropout_rate=text_encoder_attention_dropout_rate,
            conformer_kernel_size=text_encoder_conformer_kernel_size,
            use_macaron_style=use_macaron_style_in_text_encoder,
            use_conformer_conv=use_conformer_conv_in_text_encoder,
            midi_dim=midi_dim,
            beat_dim=beat_dim,
        )

        self.decoder = HiFiGANGenerator(
            in_channels=hidden_channels,
            out_channels=1,
            channels=decoder_channels,
            global_channels=global_channels,
            kernel_size=decoder_kernel_size,
            upsample_scales=decoder_upsample_scales,
            upsample_kernel_sizes=decoder_upsample_kernel_sizes,
            resblock_kernel_sizes=decoder_resblock_kernel_sizes,
            resblock_dilations=decoder_resblock_dilations,
            use_weight_norm=use_weight_norm_in_decoder,
        )
        self.posterior_encoder = PosteriorEncoder(
            in_channels=aux_channels,
            out_channels=hidden_channels,
            hidden_channels=hidden_channels,
            kernel_size=posterior_encoder_kernel_size,
            layers=posterior_encoder_layers,
            stacks=posterior_encoder_stacks,
            base_dilation=posterior_encoder_base_dilation,
            global_channels=global_channels,
            dropout_rate=posterior_encoder_dropout_rate,
            use_weight_norm=use_weight_norm_in_posterior_encoder,
        )
        self.flow = ResidualAffineCouplingBlock(
            in_channels=hidden_channels,
            hidden_channels=hidden_channels,
            flows=flow_flows,
            kernel_size=flow_kernel_size,
            base_dilation=flow_base_dilation,
            layers=flow_layers,
            global_channels=global_channels,
            dropout_rate=flow_dropout_rate,
            use_weight_norm=use_weight_norm_in_flow,
            use_only_mean=use_only_mean_in_flow,
        )
        self.project = Projection(hidden_channels, hidden_channels)
        # TODO(kan-bayashi): Add deterministic version as an option
        self.use_sdp = use_sdp
        if use_sdp:
            self.duration_predictor = StochasticDurationPredictor(
                channels=hidden_channels,
                kernel_size=stochastic_duration_predictor_kernel_size,
                dropout_rate=stochastic_duration_predictor_dropout_rate,
                flows=stochastic_duration_predictor_flows,
                dds_conv_layers=stochastic_duration_predictor_dds_conv_layers,
                global_channels=global_channels,
            )
        else:
            self.duration_predictor = DurationPredictor(
                channels=hidden_channels,
                filter_channels=256,
                kernel_size=3,
                dropout_rate=0.5,
                gin_channels=0,
            )

        self.lr = LengthRegulator()

        self.upsample_factor = int(np.prod(decoder_upsample_scales))
        self.spks = None
        if spks is not None and spks > 1:
            assert global_channels > 0
            self.spks = spks
            self.global_emb = torch.nn.Embedding(spks, global_channels)
        self.spk_embed_dim = None
        if spk_embed_dim is not None and spk_embed_dim > 0:
            assert global_channels > 0
            self.spk_embed_dim = spk_embed_dim
            self.spemb_proj = torch.nn.Linear(spk_embed_dim, global_channels)
        self.langs = None
        if langs is not None and langs > 1:
            assert global_channels > 0
            self.langs = langs
            self.lang_emb = torch.nn.Embedding(langs, global_channels)

        # delayed import
        from espnet2.gan_tts.vits.monotonic_align import maximum_path

        self.maximum_path = maximum_path

    def forward(
        self,
        text: torch.Tensor,
        text_lengths: torch.Tensor,
        feats: torch.Tensor,
        feats_lengths: torch.Tensor,
        ds: torch.Tensor,
        label_lab: Optional[torch.Tensor] = None,
        label_lab_lengths: Optional[torch.Tensor] = None,
        label_xml: Optional[torch.Tensor] = None,
        label_xml_lengths: Optional[torch.Tensor] = None,
        midi_lab: Optional[torch.Tensor] = None,
        midi_lab_lengths: Optional[torch.Tensor] = None,
        midi_xml: Optional[torch.Tensor] = None,
        midi_xml_lengths: Optional[torch.Tensor] = None,
        tempo_lab: Optional[torch.Tensor] = None,
        tempo_lab_lengths: Optional[torch.Tensor] = None,
        tempo_xml: Optional[torch.Tensor] = None,
        tempo_xml_lengths: Optional[torch.Tensor] = None,
        beat_lab: Optional[torch.Tensor] = None,
        beat_lab_lengths: Optional[torch.Tensor] = None,
        beat_xml: Optional[torch.Tensor] = None,
        beat_xml_lengths: Optional[torch.Tensor] = None,
        sids: Optional[torch.Tensor] = None,
        spembs: Optional[torch.Tensor] = None,
        lids: Optional[torch.Tensor] = None,
    ) -> Tuple[
        torch.Tensor,
        torch.Tensor,
        torch.Tensor,
        torch.Tensor,
        torch.Tensor,
        torch.Tensor,
        Tuple[
            torch.Tensor,
            torch.Tensor,
            torch.Tensor,
            torch.Tensor,
            torch.Tensor,
            torch.Tensor,
        ],
    ]:
        """Calculate forward propagation.

        Args:
            text (Tensor): Text index tensor (B, T_text).
            text_lengths (Tensor): Text length tensor (B,).
            feats (Tensor): Feature tensor (B, aux_channels, T_feats).
            feats_lengths (Tensor): Feature length tensor (B,).
            sids (Optional[Tensor]): Speaker index tensor (B,) or (B, 1).
            spembs (Optional[Tensor]): Speaker embedding tensor (B, spk_embed_dim).
            lids (Optional[Tensor]): Language index tensor (B,) or (B, 1).

        Returns:
            Tensor: Waveform tensor (B, 1, segment_size * upsample_factor).
            Tensor: Duration negative log-likelihood (NLL) tensor (B,).
            Tensor: Monotonic attention weight tensor (B, 1, T_feats, T_text).
            Tensor: Segments start index tensor (B,).
            Tensor: Text mask tensor (B, 1, T_text).
            Tensor: Feature mask tensor (B, 1, T_feats).
            tuple[Tensor, Tensor, Tensor, Tensor, Tensor, Tensor]:
                - Tensor: Posterior encoder hidden representation (B, H, T_feats).
                - Tensor: Flow hidden representation (B, H, T_feats).
                - Tensor: Expanded text encoder projected mean (B, H, T_feats).
                - Tensor: Expanded text encoder projected scale (B, H, T_feats).
                - Tensor: Posterior encoder projected mean (B, H, T_feats).
                - Tensor: Posterior encoder projected scale (B, H, T_feats).

        """
        # forward text encoder
        # print("label_xml", label_xml.shape)
        # print("label_xml_lengths", label_xml_lengths)
        # print("feats", feats.shape)
        # print("feats_lengths", feats_lengths)
        # print("label_xml_lengths", label_xml_lengths)
        for i, length in enumerate(label_xml_lengths):
            if length == label_xml.shape[1]:
                label_xml_lengths[i] = feats.shape[2]
        if label_xml.shape[1] < feats.shape[2]:
            label_xml = F.pad(
                input=label_xml,
                pad=(0, feats.shape[2] - label_xml.shape[1], 0, 0),
                mode="constant",
                value=0,
            )
            midi_xml = F.pad(
                input=midi_xml,
                pad=(0, feats.shape[2] - midi_xml.shape[1], 0, 0),
                mode="constant",
                value=0,
            )
            beat_xml = F.pad(
                input=beat_xml,
                pad=(0, feats.shape[2] - beat_xml.shape[1], 0, 0),
                mode="constant",
                value=0,
            )
        else:
            label_xml = label_xml[:, : feats.shape[2]]
            midi_xml = midi_xml[:, : feats.shape[2]]
            beat_xml = beat_xml[:, : feats.shape[2]]
        # print("label_xml_lengths222", label_xml_lengths)
        # print("label_xml22222", label_xml.shape)
        x, m_p, logs_p, x_mask = self.text_encoder(
            label_xml, label_xml_lengths, ds, midi_xml, beat_xml
        )
        # print("m_p shape1", m_p.shape)
        # print("logs_p shape1", logs_p.shape)
        # calculate global conditioning
        g = None
        if self.spks is not None:
            # speaker one-hot vector embedding: (B, global_channels, 1)
            g = self.global_emb(sids.view(-1)).unsqueeze(-1)
        if self.spk_embed_dim is not None:
            # pretreined speaker embedding, e.g., X-vector (B, global_channels, 1)
            g_ = self.spemb_proj(F.normalize(spembs)).unsqueeze(-1)
            if g is None:
                g = g_
            else:
                g = g + g_
        if self.langs is not None:
            # language one-hot vector embedding: (B, global_channels, 1)
            g_ = self.lang_emb(lids.view(-1)).unsqueeze(-1)
            if g is None:
                g = g_
            else:
                g = g + g_

        # w = beat_xml.unsqueeze(1)
        # logw_ = w * x_mask
        # logw = self.duration_predictor(x, x_mask, beat_xml, g=g)
        # logw = torch.mul(logw.squeeze(1), beat_xml).unsqueeze(1)
        # dur_nll = torch.sum((logw - logw_) ** 2, [1, 2])
        # x_frame, frame_pitch, x_lengths = self.lr(
        #     x, midi_xml, beat_xml, beat_xml_lengths
        # )
        # x_frame = x_frame.to(x.device)

        # x_mask = torch.unsqueeze(sequence_mask(x_lengths, x_frame.size(2)), 1).to(
        #     x.dtype
        # )  # 更新x_mask矩阵
        # x_mask = x_mask.to(x.device)

        # # position encoding
        # # TODO: modify PositionalEncoding, shape and transpose problem
        # max_len = x_frame.size(2)
        # d_model = x_frame.size(1)
        # batch_size = x_frame.size(0)

        # pe = torch.zeros(batch_size, max_len, d_model)
        # position = torch.arange(0, max_len).unsqueeze(1)
        # div_term = torch.exp(
        #     torch.arange(0, d_model, 2) * -(math.log(10000.0) / d_model)
        # )
        # pe[:, :, 0::2] = torch.sin(position * div_term)
        # pe[:, :, 1::2] = torch.cos(position * div_term)
        # pe = pe.transpose(1, 2).to(x_frame.device)
        # x_frame = x_frame + pe

        # self.pos_encoder = PositionalEncoding(
        #     d_model=x_frame.size(1), dropout_rate=0.1, max_len=x_frame.size(2)
        # )
        # x_frame = self.pos_encoder(x_frame)

        # m_p, logs_p = self.project(x_frame, x_mask)

        # x_mask = torch.unsqueeze(sequence_mask(label_xml_lengths, x.size(2)), 1).to(
        #     x.dtype
        # )  # 更新x_mask矩阵
        # x_mask = x_mask.to(x.device)
        # print("x", x.shape)
        # print("xmask", x_mask.shape)

        # position encoding
        max_len = x.size(2)
        d_model = x.size(1)
        batch_size = x.size(0)

        pe = torch.zeros(batch_size, max_len, d_model)
        position = torch.arange(0, max_len).unsqueeze(1)
        div_term = torch.exp(
            torch.arange(0, d_model, 2) * -(math.log(10000.0) / d_model)
        )
        pe[:, :, 0::2] = torch.sin(position * div_term)
        pe[:, :, 1::2] = torch.cos(position * div_term)
        pe = pe.transpose(1, 2).to(x.device)
        x = x + pe
        # m_p, logs_p = self.project(x, x_mask)

        # print("m_p shape", m_p.shape)
        # print("logs_p shape", logs_p.shape)
        # forward posterior encoder
        z, m_q, logs_q, y_mask = self.posterior_encoder(feats, feats_lengths, g=g)
        # print("m_q shape", m_q.shape)
        # print("logs_q shape", logs_q.shape)
        # forward flow
        z_p = self.flow(z, y_mask, g=g)  # (B, H, T_feats)

        # # monotonic alignment search
        # with torch.no_grad():
        #     # negative cross-entropy
        #     s_p_sq_r = torch.exp(-2 * logs_p)  # (B, H, T_text)
        #     # (B, 1, T_text)
        #     neg_x_ent_1 = torch.sum(
        #         -0.5 * math.log(2 * math.pi) - logs_p,
        #         [1],
        #         keepdim=True,
        #     )
        #     # (B, T_feats, H) x (B, H, T_text) = (B, T_feats, T_text)
        #     neg_x_ent_2 = torch.matmul(
        #         -0.5 * (z_p**2).transpose(1, 2),
        #         s_p_sq_r,
        #     )
        #     # (B, T_feats, H) x (B, H, T_text) = (B, T_feats, T_text)
        #     neg_x_ent_3 = torch.matmul(
        #         z_p.transpose(1, 2),
        #         (m_p * s_p_sq_r),
        #     )
        #     # (B, 1, T_text)
        #     neg_x_ent_4 = torch.sum(
        #         -0.5 * (m_p**2) * s_p_sq_r,
        #         [1],
        #         keepdim=True,
        #     )
        #     # (B, T_feats, T_text)
        #     neg_x_ent = neg_x_ent_1 + neg_x_ent_2 + neg_x_ent_3 + neg_x_ent_4
        #     # (B, 1, T_feats, T_text)
        #     attn_mask = torch.unsqueeze(x_mask, 2) * torch.unsqueeze(y_mask, -1)
        #     # monotonic attention weight: (B, 1, T_feats, T_text)
        #     attn = (
        #         self.maximum_path(
        #             neg_x_ent,
        #             attn_mask.squeeze(1),
        #         )
        #         .unsqueeze(1)
        #         .detach()
        #     )

        # # forward duration predictor
        # w = attn.sum(2)  # (B, 1, T_text)
        # dur_nll = self.duration_predictor(x, x_mask, w=w, g=g)
        # dur_nll = dur_nll / torch.sum(x_mask)

        # expand the length to match with the feature sequence
        # (B, T_feats, T_text) x (B, T_text, H) -> (B, H, T_feats)
        # m_p = torch.matmul(attn.squeeze(1), m_p.transpose(1, 2)).transpose(1, 2)
        # (B, T_feats, T_text) x (B, T_text, H) -> (B, H, T_feats)
        # logs_p = torch.matmul(attn.squeeze(1), logs_p.transpose(1, 2)).transpose(1, 2)

        # get random segments
        z_segments, z_start_idxs = get_random_segments(
            z,
            feats_lengths,
            self.segment_size,
        )

        # forward decoder with random segments
        wav = self.decoder(z_segments, g=g)

        return (
            wav,
            z_start_idxs,
            x_mask,
            y_mask,
            (z, z_p, m_p, logs_p, m_q, logs_q),
        )

    def inference(
        self,
        text: torch.Tensor,
        text_lengths: torch.Tensor,
        feats: Optional[torch.Tensor] = None,
        feats_lengths: Optional[torch.Tensor] = None,
        label_lab: Optional[torch.Tensor] = None,
        label_xml: Optional[torch.Tensor] = None,
        label_xml_lengths: Optional[torch.Tensor] = None,
        midi_lab: Optional[torch.Tensor] = None,
        midi_xml: Optional[torch.Tensor] = None,
        tempo_lab: Optional[torch.Tensor] = None,
        tempo_xml: Optional[torch.Tensor] = None,
        beat_lab: Optional[torch.Tensor] = None,
        beat_xml: Optional[torch.Tensor] = None,
        sids: Optional[torch.Tensor] = None,
        spembs: Optional[torch.Tensor] = None,
        lids: Optional[torch.Tensor] = None,
        dur: Optional[torch.Tensor] = None,
        noise_scale: float = 0.667,
        noise_scale_dur: float = 0.8,
        alpha: float = 1.0,
        max_len: Optional[int] = None,
        use_teacher_forcing: bool = False,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Run inference.

        Args:
            text (Tensor): Input text index tensor (B, T_text,).
            text_lengths (Tensor): Text length tensor (B,).
            feats (Tensor): Feature tensor (B, aux_channels, T_feats,).
            feats_lengths (Tensor): Feature length tensor (B,).
            sids (Optional[Tensor]): Speaker index tensor (B,) or (B, 1).
            spembs (Optional[Tensor]): Speaker embedding tensor (B, spk_embed_dim).
            lids (Optional[Tensor]): Language index tensor (B,) or (B, 1).
            dur (Optional[Tensor]): Ground-truth duration (B, T_text,). If provided,
                skip the prediction of durations (i.e., teacher forcing).
            noise_scale (float): Noise scale parameter for flow.
            noise_scale_dur (float): Noise scale parameter for duration predictor.
            alpha (float): Alpha parameter to control the speed of generated speech.
            max_len (Optional[int]): Maximum length of acoustic feature sequence.
            use_teacher_forcing (bool): Whether to use teacher forcing.

        Returns:
            Tensor: Generated waveform tensor (B, T_wav).
            Tensor: Monotonic attention weight tensor (B, T_feats, T_text).
            Tensor: Duration tensor (B, T_text).

        """
        # encoder
        x, m_p, logs_p, x_mask = self.text_encoder(
            label_xml, label_xml_lengths, None, midi_xml, beat_xml
        )
        g = None
        if self.spks is not None:
            # (B, global_channels, 1)
            g = self.global_emb(sids.view(-1)).unsqueeze(-1)
        if self.spk_embed_dim is not None:
            # (B, global_channels, 1)
            g_ = self.spemb_proj(F.normalize(spembs.unsqueeze(0))).unsqueeze(-1)
            if g is None:
                g = g_
            else:
                g = g + g_
        if self.langs is not None:
            # (B, global_channels, 1)
            g_ = self.lang_emb(lids.view(-1)).unsqueeze(-1)
            if g is None:
                g = g_
            else:
                g = g + g_

        if use_teacher_forcing:
            # forward posterior encoder
            z, m_q, logs_q, y_mask = self.posterior_encoder(feats, feats_lengths, g=g)

            # forward flow
            z_p = self.flow(z, y_mask, g=g)  # (B, H, T_feats)

            # # monotonic alignment search
            # s_p_sq_r = torch.exp(-2 * logs_p)  # (B, H, T_text)
            # # (B, 1, T_text)
            # neg_x_ent_1 = torch.sum(
            #     -0.5 * math.log(2 * math.pi) - logs_p,
            #     [1],
            #     keepdim=True,
            # )
            # # (B, T_feats, H) x (B, H, T_text) = (B, T_feats, T_text)
            # neg_x_ent_2 = torch.matmul(
            #     -0.5 * (z_p**2).transpose(1, 2),
            #     s_p_sq_r,
            # )
            # # (B, T_feats, H) x (B, H, T_text) = (B, T_feats, T_text)
            # neg_x_ent_3 = torch.matmul(
            #     z_p.transpose(1, 2),
            #     (m_p * s_p_sq_r),
            # )
            # # (B, 1, T_text)
            # neg_x_ent_4 = torch.sum(
            #     -0.5 * (m_p**2) * s_p_sq_r,
            #     [1],
            #     keepdim=True,
            # )
            # # (B, T_feats, T_text)
            # neg_x_ent = neg_x_ent_1 + neg_x_ent_2 + neg_x_ent_3 + neg_x_ent_4
            # # (B, 1, T_feats, T_text)
            # attn_mask = torch.unsqueeze(x_mask, 2) * torch.unsqueeze(y_mask, -1)
            # # monotonic attention weight: (B, 1, T_feats, T_text)
            # attn = self.maximum_path(
            #     neg_x_ent,
            #     attn_mask.squeeze(1),
            # ).unsqueeze(1)
            # dur = attn.sum(2)  # (B, 1, T_text)

            # forward decoder with random segments
            wav = self.decoder(z * y_mask, g=g)
        else:
            # # duration
            # if dur is None:
            #     logw = self.duration_predictor(
            #         x,
            #         x_mask,
            #         g=g,
            #         inverse=True,
            #         noise_scale=noise_scale_dur,
            #     )
            #     w = torch.exp(logw) * x_mask * alpha
            #     dur = torch.ceil(w)
            # y_lengths = torch.clamp_min(torch.sum(dur, [1, 2]), 1).long()
            # y_mask = make_non_pad_mask(y_lengths).unsqueeze(1).to(text.device)
            # attn_mask = torch.unsqueeze(x_mask, 2) * torch.unsqueeze(y_mask, -1)
            # attn = self._generate_path(dur, attn_mask)

            # # expand the length to match with the feature sequence
            # # (B, T_feats, T_text) x (B, T_text, H) -> (B, H, T_feats)
            # m_p = torch.matmul(
            #     attn.squeeze(1),
            #     m_p.transpose(1, 2),
            # ).transpose(1, 2)
            # # (B, T_feats, T_text) x (B, T_text, H) -> (B, H, T_feats)
            # logs_p = torch.matmul(
            #     attn.squeeze(1),
            #     logs_p.transpose(1, 2),
            # ).transpose(1, 2)

            # decoder
            z_p = m_p + torch.randn_like(m_p) * torch.exp(logs_p) * noise_scale
            z = self.flow(z_p, x_mask, g=g, inverse=True)
            wav = self.decoder((z * x_mask)[:, :, :max_len], g=g)

        # return wav.squeeze(1), attn.squeeze(1), dur.squeeze(1)
        return wav.squeeze(1), None, None
        # return wav.squeeze(1)

    def _generate_path(self, dur: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
        """Generate path a.k.a. monotonic attention.

        Args:
            dur (Tensor): Duration tensor (B, 1, T_text).
            mask (Tensor): Attention mask tensor (B, 1, T_feats, T_text).

        Returns:
            Tensor: Path tensor (B, 1, T_feats, T_text).

        """
        b, _, t_y, t_x = mask.shape
        cum_dur = torch.cumsum(dur, -1)
        cum_dur_flat = cum_dur.view(b * t_x)
        path = torch.arange(t_y, dtype=dur.dtype, device=dur.device)
        path = path.unsqueeze(0) < cum_dur_flat.unsqueeze(1)
        path = path.view(b, t_x, t_y).to(dtype=mask.dtype)
        # path will be like (t_x = 3, t_y = 5):
        # [[[1., 1., 0., 0., 0.],      [[[1., 1., 0., 0., 0.],
        #   [1., 1., 1., 1., 0.],  -->   [0., 0., 1., 1., 0.],
        #   [1., 1., 1., 1., 1.]]]       [0., 0., 0., 0., 1.]]]
        path = path - F.pad(path, [0, 0, 1, 0, 0, 0])[:, :-1]
        return path.unsqueeze(1).transpose(2, 3) * mask
