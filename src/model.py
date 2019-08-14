from __future__ import unicode_literals, print_function, division

import config
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence

use_cuda = config.use_gpu and torch.cuda.is_available()
torch.manual_seed(123)
if torch.cuda.is_available():
    torch.cuda.manual_seed_all(123)

def init_lstm_wt(lstm):
    for names in lstm._all_weights:
        for name in names:
            if name.startswith('weight_'):
                wt = getattr(lstm, name)
                wt.data.uniform_(-config.rand_unif_init_mag, config.rand_unif_init_mag)
            elif name.startswith('bias_'):
                # set forget bias to 1
                bias = getattr(lstm, name)
                n = bias.size(0)
                start, end = n // 4, n // 2
                bias.data.fill_(0.)
                bias.data[start:end].fill_(1.)

def init_linear_wt(linear):
    linear.weight.data.normal_(std=config.trunc_norm_init_std)
    if linear.bias is not None:
        linear.bias.data.normal_(std=config.trunc_norm_init_std)

def init_wt_normal(wt):
    wt.data.normal_(std=config.trunc_norm_init_std)

def init_wt_unif(wt):
    wt.data.uniform_(-config.rand_unif_init_mag, config.rand_unif_init_mag)


class Encoder(nn.Module):
    def __init__(self):
        super(Encoder, self).__init__()
        self.embedding = nn.Embedding(config.vocab_size, config.emb_dim)
        init_wt_normal(self.embedding.weight)
        self.lstm = nn.LSTM(config.emb_dim, config.hidden_dim, num_layers=1, 
                            batch_first=True, bidirectional=True)
        init_lstm_wt(self.lstm)
        self.W_h = nn.Linear(config.hidden_dim * 2, config.hidden_dim * 2, bias=False)

    # seq_lens should be in descending order
    def forward(self, input, seq_lens):
        embedded = self.embedding(input)
        packed = pack_padded_sequence(embedded, seq_lens, batch_first=True)
        output, hidden = self.lstm(packed) # hidden = ((2 x B x H), (2 x B x H))
        encoder_outputs, _ = pad_packed_sequence(output, batch_first=True) # B x L x 2H
        encoder_feature = encoder_outputs.contiguous().view(-1, 2*config.hidden_dim) # BL x 2H
        encoder_feature = self.W_h(encoder_feature) # BL x 2H
        return encoder_outputs, encoder_feature, hidden


class ReduceState(nn.Module):
    def __init__(self):
        super(ReduceState, self).__init__()
        self.reduce_h = nn.Linear(config.hidden_dim * 2, config.hidden_dim)
        init_linear_wt(self.reduce_h)
        self.reduce_c = nn.Linear(config.hidden_dim * 2, config.hidden_dim)
        init_linear_wt(self.reduce_c)

    def forward(self, hidden):
        h, c = hidden
        h_in = h.transpose(0, 1).contiguous().view(-1, config.hidden_dim * 2) # B x 2H
        hidden_reduced_h = F.relu(self.reduce_h(h_in))  # B x H
        c_in = c.transpose(0, 1).contiguous().view(-1, config.hidden_dim * 2)
        hidden_reduced_c = F.relu(self.reduce_c(c_in))  # B x H
        return (hidden_reduced_h.unsqueeze(0), hidden_reduced_c.unsqueeze(0)) # 1 x B x H


class Attention(nn.Module):
    def __init__(self):
        super(Attention, self).__init__()
        if config.is_coverage:
            self.W_c = nn.Linear(1, config.hidden_dim * 2, bias=False)
        self.decode_proj = nn.Linear(config.hidden_dim * 2, config.hidden_dim * 2)
        self.v = nn.Linear(config.hidden_dim * 2, 1, bias=False)

    def forward(self, s_t_hat, encoder_outputs, encoder_feature, enc_padding_mask, coverage):
        b, t_k, n = list(encoder_outputs.size())

        dec_fea = self.decode_proj(s_t_hat) # B x 2H
        dec_fea_expanded = dec_fea.unsqueeze(1).expand(b, t_k, n).contiguous() # B x L x 2H
        dec_fea_expanded = dec_fea_expanded.view(-1, n)  # BL x 2H

        att_features = encoder_feature + dec_fea_expanded # BL x 2H
        if config.is_coverage:
            coverage_input = coverage.view(-1, 1)  # BL x 1
            coverage_feature = self.W_c(coverage_input)  # BL x 2H
            att_features = att_features + coverage_feature

        e = torch.tanh(att_features) # BL x 2H
        scores = self.v(e)  # BL x 1
        scores = scores.view(-1, t_k)  # B x L
        
        # NOTE: Here may lead to NAN problem!!!
        attn_dist_ = F.softmax(scores, dim=1) * enc_padding_mask # B x L
        normalization_factor = attn_dist_.sum(1, keepdim=True)
        attn_dist = attn_dist_ / normalization_factor

        attn_dist = attn_dist.unsqueeze(1)  # B x 1 x L
        c_t = torch.bmm(attn_dist, encoder_outputs)  # B x 1 x 2H
        c_t = c_t.view(-1, config.hidden_dim * 2)  # B x 2H

        attn_dist = attn_dist.view(-1, t_k)  # B x L

        if config.is_coverage:
            coverage = coverage.view(-1, t_k)
            coverage = coverage + attn_dist  # B x L
        return c_t, attn_dist, coverage


class Decoder(nn.Module):
    def __init__(self):
        super(Decoder, self).__init__()
        self.attention_network = Attention()
        self.embedding = nn.Embedding(config.vocab_size, config.emb_dim)
        init_wt_normal(self.embedding.weight)
        self.x_context = nn.Linear(config.hidden_dim * 2 + config.emb_dim, config.emb_dim)
        self.lstm = nn.LSTM(config.emb_dim, config.hidden_dim, num_layers=1, 
                            batch_first=True, bidirectional=False)
        init_lstm_wt(self.lstm)
        
        if config.pointer_gen:
            self.p_gen_linear = nn.Linear(config.hidden_dim * 4 + config.emb_dim, 1)

        # p_vocab
        self.out1 = nn.Linear(config.hidden_dim * 3, config.hidden_dim)
        self.out2 = nn.Linear(config.hidden_dim, config.vocab_size)
        init_linear_wt(self.out2)

    def forward(self, y_t_1, s_t_1, encoder_outputs, encoder_feature, enc_padding_mask,
                c_t_1, extra_zeros, enc_batch_extend_vocab, coverage, step):
        if not self.training and step == 0:
            h_decoder, c_decoder = s_t_1
            s_t_hat = torch.cat((h_decoder.view(-1, config.hidden_dim),
                                 c_decoder.view(-1, config.hidden_dim)), 1)  # B x 2H
            c_t, _, coverage_next = self.attention_network(s_t_hat, encoder_outputs, encoder_feature,
                                                           enc_padding_mask, coverage)
            coverage = coverage_next  # B x L

        y_t_1_embd = self.embedding(y_t_1) #  B x E
        x = self.x_context(torch.cat((c_t_1, y_t_1_embd), 1)) # B x E
        lstm_out, s_t = self.lstm(x.unsqueeze(1), s_t_1)

        h_decoder, c_decoder = s_t
        s_t_hat = torch.cat((h_decoder.view(-1, config.hidden_dim),
                             c_decoder.view(-1, config.hidden_dim)), 1)  # B x 2H
        c_t, attn_dist, coverage_next = self.attention_network(s_t_hat, encoder_outputs, encoder_feature,
                                                               enc_padding_mask, coverage)

        if self.training or step > 0:
            coverage = coverage_next

        p_gen = None
        if config.pointer_gen:
            p_gen_input = torch.cat((c_t, s_t_hat, x), 1)  # B x (4H + E)
            p_gen = self.p_gen_linear(p_gen_input)
            p_gen = torch.sigmoid(p_gen)  # B x 1

        output = torch.cat((lstm_out.view(-1, config.hidden_dim), c_t), 1) # B x 3H
        output = self.out1(output) # B x H
        output = self.out2(output) # B x V
        vocab_dist = F.softmax(output, dim=1)

        if config.pointer_gen:
            vocab_dist_ = p_gen * vocab_dist
            attn_dist_ = (1 - p_gen) * attn_dist
            if extra_zeros is not None:
                vocab_dist_ = torch.cat([vocab_dist_, extra_zeros], 1)
            final_dist = vocab_dist_.scatter_add(1, enc_batch_extend_vocab, attn_dist_)
        else:
            final_dist = vocab_dist
        return final_dist, s_t, c_t, attn_dist, p_gen, coverage


class Model(object):
    def __init__(self, model_file_path=None, is_eval=False):
        encoder = Encoder()
        decoder = Decoder()
        reduce_state = ReduceState()
        # shared the embedding between encoder and decoder
        decoder.embedding.weight = encoder.embedding.weight
        
        if is_eval:
            self.encoder = encoder.eval()
            self.decoder = decoder.eval()
            self.reduce_state = reduce_state.eval()
        else:
            self.encoder = encoder.train()
            self.decoder = decoder.train()
            self.reduce_state = reduce_state.train()

        if use_cuda:
            self.encoder = encoder.cuda()
            self.decoder = decoder.cuda()
            self.reduce_state = reduce_state.cuda()

        if model_file_path is not None:
            state = torch.load(model_file_path, map_location= lambda storage, location: storage)
            self.encoder.load_state_dict(state['encoder_state_dict'])
            self.decoder.load_state_dict(state['decoder_state_dict'], strict=False)
            self.reduce_state.load_state_dict(state['reduce_state_dict'])