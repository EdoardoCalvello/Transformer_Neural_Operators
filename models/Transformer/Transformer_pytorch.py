import torch
import torch.nn as nn

from torch.nn import TransformerEncoder
from torch.nn import TransformerEncoderLayer

# Define the neural network model
class SimpleEncoder(torch.nn.Module):
    def __init__(self, input_dim=1, output_dim=1, domain_dim=1, d_model=32, nhead=8, num_layers=6,
                 learning_rate=0.01, max_sequence_length=100,
                 do_layer_norm=True,
                 use_transformer=True,
                 use_positional_encoding='continuous',
                 append_position_to_x=False,
                 pos_enc_coeff=2,
                 include_y0_input=False,
                 activation='relu',
                 dropout=0.1, norm_first=False, dim_feedforward=2048):
        super(SimpleEncoder, self).__init__()
        self.input_dim = input_dim
        self.output_dim = output_dim
        self.domain_dim = domain_dim
        self.d_model = d_model
        self.learning_rate = learning_rate
        self.max_sequence_length = max_sequence_length
        self.use_transformer = use_transformer
        self.use_positional_encoding = use_positional_encoding
        self.append_position_to_x = append_position_to_x
        self.pos_enc_coeff = pos_enc_coeff # coefficient for positional encoding
        self.include_y0_input = include_y0_input # whether to use y as input to the encoder

        self.set_positional_encoding()

        encoder_layer = TransformerEncoderLayer(
            d_model=d_model, nhead=nhead,
            dropout=dropout,
            activation=activation,
            norm_first=norm_first,
            #do_layer_norm=do_layer_norm,
            dim_feedforward=dim_feedforward,
            batch_first=True)  # when batch first, expects input tensor (batch_size, Seq_len, input_dim)
        self.encoder = TransformerEncoder(
            encoder_layer, num_layers=num_layers)
        # (Seq_len,batch_size,input_dim) if batch_first=False or (N, S, E) if batch_first=True.
        # where S is the source sequence length, N is the batch size, E is the feature number, T is the target sequence length,

        self.linear_in = nn.Linear(input_dim, d_model)
        #linear layer to transform the input to the right dimension if positions are appended to input
        self.linear_in_position = nn.Linear(input_dim + domain_dim, d_model)
        self.linear_out = nn.Linear(d_model, output_dim)

    def set_positional_encoding(self):
        pe = torch.zeros(self.max_sequence_length, self.d_model)
        position = torch.arange(
            0, self.max_sequence_length, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(torch.arange(
            0, self.d_model, 2, dtype=torch.float) * -(torch.log(torch.tensor(10000.0)) / self.d_model))
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0)  # Add a batch dimension
        self.register_buffer('pe_discrete', pe)

        # for continuous time positional encoding (assumes square spatial domain)
        even_inds = torch.arange(0, self.d_model, 2).unsqueeze(0)
        odd_inds = torch.arange(1, self.d_model, 2).unsqueeze(0)
        self.register_buffer('even_inds', even_inds)
        self.register_buffer('odd_inds', odd_inds)

    def pe_continuous(self, coords):
        '''generate the positional encoding for coords'''
        # .to() sends the tensor to the device of the argument
        pe = torch.zeros(coords.shape[0], self.d_model).to(coords)
        pe[:, 0::2] = torch.sin(10**self.pos_enc_coeff * coords[:,0] * 10**(-4 * self.even_inds / self.d_model))
        pe[:, 1::2] = torch.cos(10**self.pos_enc_coeff * coords[:,0] * 10**(-4 * self.odd_inds / self.d_model))
        for i in range(1, self.domain_dim):
            pe[:, 0::2] = pe[:, 0::2] * torch.sin(10**self.pos_enc_coeff * coords[:,i] * 10**(-4 * self.even_inds / self.d_model))
            pe[:, 1::2] = pe[:, 1::2] * torch.cos(10**self.pos_enc_coeff * coords[:,i] * 10**(-4 * self.odd_inds / self.d_model))
        return pe

    def positional_encoding(self, x, coords):
        # x: (batch_size, seq_len, input_dim)
        # pe: (1, seq_len, d_model)
        # x + pe[:, :x.size(1)]  # (batch_size, seq_len, d_model)
        if self.use_positional_encoding=='discrete':
            pe = self.pe_discrete[:, :x.size(1)]
        elif self.use_positional_encoding=='continuous':
            pe = self.pe_continuous(coords)
        else: # no positional encoding
            # .to() sends the tensor to the device of the argument
            pe = torch.zeros(x.shape).to(x)

        return pe

    def apply_positional_encoding(self, x, coords):
        pe = self.positional_encoding(x, coords)
        if self.include_y0_input:
            x[:, self.output_dim:, :] += pe
            #'include_y0_input': ['uniform', 'staggered', False],
            if self.include_y0_input == 'uniform':
                x[:, :self.output_dim, :] += torch.tensor(2).to(x)
            elif self.include_y0_input == 'staggered':
                x[:, :self.output_dim, :] += torch.arange(2, self.output_dim+2).unsqueeze(0).unsqueeze(2).to(x)
            else:
                raise ValueError('include_y0_input must be one of [uniform, staggered, False]')
        else:
            x += pe
        return x

    def forward(self, x, y=None, coords_x=None, coords_y=None):
        if self.include_y0_input:
            #this only works when the input dimension is 1, indeed how would you concatenate initial condition with the input otherwise?
            # x = x.permute(1,0,2) # (seq_len, batch_size, dim_state)
            # reshape to make sure we have dim (batch,1,dim_output)
            initial_cond = y[:, 0:1, :].permute(0, 2, 1)  # (batch_size, dim_state, 1)
            #flip to make sure that x_0 is given as the dim_output entry of the seq_len dimension
            initial_cond = torch.flip(initial_cond, dims=[-2])
            # (batch_size, seq_len+output_dim, dim_state)
            x = torch.cat((initial_cond, x), dim=1)

            #now make sure coords has the appropriate values
            initial_cond_coords = torch.zeros((self.output_dim,1,1)).to(x)
            coords_x = torch.cat((initial_cond_coords,coords_x),dim=0)

        if self.append_position_to_x:
            append = coords_x.permute(2,0,1).repeat(x.shape[0],1,1)
            x = torch.cat((x, append), dim=2)
            x = self.linear_in_position(x)  # (batch_size, seq_len, input_dim+domain_dim)
        else:
            x = self.linear_in(x)  # (batch_size, seq_len, input_dim)

        # times = torch.linspace(0, 1, x.shape[1]).unsqueeze(1)
        # can use first time because currently all batches share the same time discretization
        if self.use_positional_encoding:
            x = self.apply_positional_encoding(x, coords_x) # coords_x is "time" for 1D case

        if self.use_transformer:
            x = self.encoder(x)  # (batch_size, seq_len, dim_state)

        x = self.linear_out(x)  # (seq_len, batch_size, output_dim)

        if self.include_y0_input:
            return x[:, self.output_dim:, :]
        else:
            return x