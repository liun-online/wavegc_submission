import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch_geometric.graphgym.register as register
import torch_geometric.nn as pygnn
from performer_pytorch import SelfAttention
from torch_geometric.data import Batch
from torch_geometric.nn import Linear as Linear_pyg
from torch_geometric.utils import to_dense_batch

from graphgps.layer.bigbird_layer import SingleBigBirdLayer
from graphgps.layer.gatedgcn_layer import GatedGCNLayer
from graphgps.layer.gine_conv_layer import GINEConvESLapPE

from torch_geometric.graphgym.config import cfg


class Wave_Conv(nn.Module):
    def __init__(self, dim_h, act, dropout, num_J, adj_dropout, layer_idx=-1):
        super().__init__()
        self.dim_h = dim_h
        self.single_out = self.dim_h//(num_J+1)
        self.single_out_last = self.dim_h - num_J*self.single_out
        self.activation = register.act_dict[act]()
        self.drop = nn.Dropout(dropout)
        self.t_number = num_J + 1

        self.lin = nn.ModuleList([nn.Linear(dim_h, self.single_out) for _ in range(self.t_number-1)])
        self.lin.append(nn.Linear(dim_h, self.single_out_last))

        # self.wavelet_conv_2 = nn.ModuleList([pygnn.SimpleConv()for _ in range(self.t_number)])
        # self.wavelet_conv_1 = nn.ModuleList([pygnn.GCNConv(dim_h, self.single_out, normalize=False)  for _ in range(self.t_number-1)])
        # self.wavelet_conv_1.append(pygnn.GCNConv(dim_h, self.single_out_last, normalize=False))
        self.fusion = nn.Linear(dim_h, dim_h)
        self.adj_dropout = nn.Dropout(adj_dropout)
        self.layer_idx = layer_idx
    
    def forward(self, x, batch):
        comb = []
        evc_dense = batch.eigenvector.unsqueeze(0)
        evc_dense_t = evc_dense.transpose(1, 2)
        curr_signals = batch.filter_signals_after # 1*sele_num*(n_scales+1)
        #diag_filter_signals_after = torch.diag_embed(curr_signals.transpose(2, 1))  # 1*(n_scales+1)*sele_num*sele_num
        diag_filter_signals_after = curr_signals.transpose(2, 1)
        for t in range(self.t_number):
            curr_sig = diag_filter_signals_after[:, t, :]
            h = self.lin[t](x)
            h = evc_dense_t @ h
            h = curr_sig.unsqueeze(-1) * h
            h = self.drop(self.activation(evc_dense @ h))

            h = evc_dense_t @ h
            h = curr_sig.unsqueeze(-1) * h
            h = evc_dense @ h
            comb.append(h)
        comb = torch.cat(comb, -1)
        final_emb = self.drop(self.activation(self.fusion(comb)))[0]
        return final_emb


class WaveSAN_large(nn.Module):
    """Local MPNN + full graph attention x-former layer.
    """

    def __init__(self, layer_num,layer_idx,dim_h, num_heads, num_J, full_graph, act='relu',
                 pna_degrees=None, equivstable_pe=False, dropout=0.0, adj_dropout=0.1,
                 attn_dropout=0.0, layer_norm=False, batch_norm=True,
                 bigbird_cfg=None, log_attn_weights=False, wave_drop=0., residual=True):
        super().__init__()

        self.in_channels = dim_h
        self.out_channels = dim_h
        self.num_heads = num_heads
        self.dropout = dropout
        self.attn_dropout = attn_dropout
        self.residual = residual
        self.layer_norm = layer_norm
        self.batch_norm = batch_norm
        
        if cfg.WaveGC.trans_use:
            if layer_idx!=0 and layer_idx!=layer_num-1:
                if cfg.WaveGC.weight_share:
                    self.self_attn = Wave_Conv(dim_h, act, wave_drop, num_J, adj_dropout)
                else:
                    self.self_attn = Wave_Conv(dim_h, act, wave_drop, num_J, adj_dropout,layer_idx-1)
                self.attn_flag = 'wavelet'
            else:
                self.self_attn = torch.nn.MultiheadAttention(
                    dim_h, num_heads, dropout=self.attn_dropout, batch_first=True)
                self.attn_flag = 'trans'
        else:
            if cfg.WaveGC.weight_share:
                self.self_attn = Wave_Conv(dim_h, act, wave_drop, num_J, adj_dropout)
            else:
                self.self_attn = Wave_Conv(dim_h, act, wave_drop, num_J, adj_dropout,layer_idx)
            self.attn_flag = 'wavelet'

        self.O_h = nn.Linear(dim_h, dim_h)

        if self.layer_norm:
            self.layer_norm1_h = nn.LayerNorm(dim_h)

        if self.batch_norm:
            self.batch_norm1_h = nn.BatchNorm1d(dim_h)

        # FFN for h
        self.FFN_h_layer1 = nn.Linear(dim_h, dim_h * 2)
        self.FFN_h_layer2 = nn.Linear(dim_h * 2, dim_h)

        if self.layer_norm:
            self.layer_norm2_h = nn.LayerNorm(dim_h)

        if self.batch_norm:
            self.batch_norm2_h = nn.BatchNorm1d(dim_h)

    def forward(self, batch):
        h = batch.x
        h_in1 = h  # for first residual connection

        # multi-head attention out
        if self.attn_flag == 'wavelet':
            h_attn_out = self.self_attn(h, batch)
        elif self.attn_flag == 'trans':
            h_dense, mask = to_dense_batch(h, batch.batch)
            h_attn_out = self._sa_block(h_dense, None, ~mask)[mask]
            
        #h_attn_out = self.attention(batch)

        # Concat multi-head outputs
        h = h_attn_out.view(-1, self.out_channels)

        h = F.dropout(h, self.dropout, training=self.training)

        h = self.O_h(h)

        if self.residual:
            h = h_in1 + h  # residual connection

        if self.layer_norm:
            h = self.layer_norm1_h(h)

        if self.batch_norm:
            h = self.batch_norm1_h(h)

        h_in2 = h  # for second residual connection

        # FFN for h
        h = self.FFN_h_layer1(h)
        h = F.relu(h)
        h = F.dropout(h, self.dropout, training=self.training)
        h = self.FFN_h_layer2(h)

        if self.residual:
            h = h_in2 + h  # residual connection

        if self.layer_norm:
            h = self.layer_norm2_h(h)

        if self.batch_norm:
            h = self.batch_norm2_h(h)

        batch.x = h
        return batch

    def __repr__(self):
        return '{}(in_channels={}, out_channels={}, heads={}, residual={})'.format(
            self.__class__.__name__,
            self.in_channels,
            self.out_channels, self.num_heads, self.residual)

    def _sa_block(self, x, attn_mask, key_padding_mask):
        """Self-attention block.
        """
        x = self.self_attn(x, x, x,
                           attn_mask=attn_mask,
                           key_padding_mask=key_padding_mask,
                           need_weights=False)[0]
        return x

    def _ff_block(self, x):
        """Feed Forward block.
        """
        x = self.ff_dropout1(self.act_fn_ff(self.ff_linear1(x)))
        return self.ff_dropout2(self.ff_linear2(x))

    def extra_repr(self):
        s = f'summary: dim_h={self.dim_h}, ' \
            f'local_gnn_type={self.local_gnn_type}, ' \
            f'global_model_type={self.global_model_type}, ' \
            f'heads={self.num_heads}'
        return s
