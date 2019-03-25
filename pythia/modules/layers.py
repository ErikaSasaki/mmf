import torch

from torch import nn
from torch.nn.utils.weight_norm import weight_norm


class GatedTanh(nn.Module):
    '''
    From: https://arxiv.org/pdf/1707.07998.pdf
    nonlinear_layer (f_a) : x\in R^m => y \in R^n
    \tilda{y} = tanh(Wx + b)
    g = sigmoid(W'x + b')
    y = \tilda(y) \circ g
    input: (N, *, in_dim)
    output: (N, *, out_dim)
    '''
    def __init__(self, in_dim, out_dim):
        super(GatedTanh, self).__init__()
        self.fc = nn.Linear(in_dim, out_dim)
        self.gate_fc = nn.Linear(in_dim, out_dim)

    def forward(self, x):
        y_tilda = torch.tanh(self.fc(x))
        gated = torch.sigmoid(self.gate_fc(x))

        # Element wise multiplication
        y = y_tilda * gated

        return y


# TODO: Do clean implementation without Sequential
class ReLUWithWeightNormFC(nn.Module):
    def __init__(self, in_dim, out_dim):
        super(ReLUWithWeightNormFC, self).__init__()

        layers = []
        layers.append(weight_norm(nn.Linear(in_dim, out_dim), dim=None))
        layers.append(nn.ReLU())
        self.layers = nn.Sequential(*layers)

    def forward(self, x):
        return self.layers(x)


class ClassifierLayer(nn.Module):
    def __init__(self, classifier_type, in_dim, out_dim, **kwargs):
        super(ClassifierLayer, self).__init__()

        if classifier_type == "weight_norm":
            self.module = WeightNormClassifier(in_dim, out_dim, **kwargs)
        elif classifier_type == "logit":
            self.module = LogitClassifier(in_dim, out_dim, **kwargs)
        elif classifier_type == "linear":
            self.module = nn.Linear(in_dim, out_dim)
        else:
            raise NotImplementedError("Unknown classifier type: %s"
                                      % classifier_type)

    def forward(self, *args, **kwargs):
        return self.module(*args, **kwargs)


class LogitClassifier(nn.Module):
    def __init__(self, in_dim, out_dim, **kwargs):
        super(LogitClassifier, self).__init__()
        input_dim = in_dim
        num_ans_candidates = out_dim
        txt_nonLinear_dim = kwargs['text_hidden_dim']
        image_nonLinear_dim = kwargs['img_hidden_dim']

        self.f_o_text = ReLUWithWeightNormFC(input_dim, txt_nonLinear_dim)
        self.f_o_image = ReLUWithWeightNormFC(input_dim, image_nonLinear_dim)
        self.linear_text = nn.Linear(txt_nonLinear_dim, num_ans_candidates)
        self.linear_image = nn.Linear(image_nonLinear_dim, num_ans_candidates)

        if 'pretrained_image' in kwargs and \
                kwargs['pretrained_text'] is not None:
            self.linear_text.weight.data.copy_(
                torch.from_numpy(kwargs['pretrained_text']))

        if 'pretrained_image' in kwargs and \
                kwargs['pretrained_image'] is not None:
            self.linear_image.weight.data.copy_(
                torch.from_numpy(kwargs['pretrained_image']))

    def forward(self, joint_embedding):
        text_val = self.linear_text(self.f_o_text(joint_embedding))
        image_val = self.linear_image(self.f_o_image(joint_embedding))
        logit_value = text_val + image_val

        return logit_value


class WeightNormClassifier(nn.Module):
    def __init__(self, in_dim, out_dim, hidden_dim, dropout):
        super(WeightNormClassifier, self).__init__()
        layers = [
            weight_norm(nn.Linear(in_dim, hidden_dim), dim=None),
            nn.ReLU(),
            nn.Dropout(dropout, inplace=True),
            weight_norm(nn.Linear(hidden_dim, out_dim), dim=None)
        ]
        self.main = nn.Sequential(*layers)

    def forward(self, x):
        logits = self.main(x)
        return logits


class Identity(nn.Module):
    def __init__(self, **kwargs):
        super(Identity, self).__init__()

    def forward(self, x):
        return x


class ModalCombineLayer(nn.Module):
    def __init__(self, combine_type, img_feat_dim, txt_emb_dim, **kwargs):
        super(ModalCombineLayer, self).__init__()
        if combine_type == "MFH":
            self.module = MFH(img_feat_dim, txt_emb_dim, **kwargs)
        elif combine_type == "non_linear_element_multiply":
            self.module = NonLinearElementMultiply(img_feat_dim, txt_emb_dim,
                                                   **kwargs)
        elif combine_type == "two_layer_element_multiply":
            self.module = TwoLayerElementMultiply(img_feat_dim, txt_emb_dim,
                                                  **kwargs)
        else:
            raise NotImplementedError("Not implemented combine type: %s"
                                      % combine_type)

        self.out_dim = self.module.out_dim

    def forward(self, *args, **kwargs):
        return self.module(*args, **kwargs)


class MfbExpand(nn.Module):
    def __init__(self, img_feat_dim, txt_emb_dim, hidden_dim, dropout):
        super(MfbExpand, self).__init__()
        self.lc_image = nn.Linear(
            in_features=img_feat_dim,
            out_features=hidden_dim)
        self.lc_ques = nn.Linear(
            in_features=txt_emb_dim,
            out_features=hidden_dim)
        self.dropout = nn.Dropout(dropout)

    def forward(self, image_feat, question_embed):
        image1 = self.lc_image(image_feat)
        ques1 = self.lc_ques(question_embed)
        if len(image_feat.data.shape) == 3:
            num_location = image_feat.data.size(1)
            ques1_expand = (
                torch.unsqueeze(ques1, 1).expand(-1, num_location, -1))
        else:
            ques1_expand = ques1
        joint_feature = image1 * ques1_expand
        joint_feature = self.dropout(joint_feature)
        return joint_feature


class MFH(nn.Module):
    def __init__(self, image_feat_dim, ques_emb_dim, **kwargs):
        super(MFH, self).__init__()
        self.mfb_expand_list = nn.ModuleList()
        self.mfb_sqz_list = nn.ModuleList()
        self.relu = nn.ReLU()

        hidden_sizes = kwargs['hidden_sizes']
        self.out_dim = int(sum(hidden_sizes) / kwargs['pool_size'])

        self.order = kwargs['order']
        self.pool_size = kwargs['pool_size']

        for i in range(self.order):
            mfb_exp_i = MfbExpand(img_feat_dim=image_feat_dim,
                                  txt_emb_dim=ques_emb_dim,
                                  hidden_dim=hidden_sizes[i],
                                  dropout=kwargs['dropout'])
            self.mfb_expand_list.append(mfb_exp_i)
            self.mfb_sqz_list.append(self.mfb_squeeze)

    def forward(self, image_feat, question_embedding):
        feature_list = []
        prev_mfb_exp = 1

        for i in range(self.order):
            mfb_exp = self.mfb_expand_list[i]
            mfb_sqz = self.mfb_sqz_list[i]
            z_exp_i = mfb_exp(image_feat, question_embedding)
            if i > 0:
                z_exp_i = prev_mfb_exp * z_exp_i
            prev_mfb_exp = z_exp_i
            z = mfb_sqz(z_exp_i)
            feature_list.append(z)

        # append at last feature
        cat_dim = len(feature_list[0].size()) - 1
        feature = torch.cat(feature_list, dim=cat_dim)
        return feature

    def mfb_squeeze(self, joint_feature):
        # joint_feature dim: N x k x dim or N x dim

        orig_feature_size = len(joint_feature.size())

        if orig_feature_size == 2:
            joint_feature = torch.unsqueeze(joint_feature, dim=1)

        batch_size, num_loc, dim = joint_feature.size()

        if dim % self.pool_size != 0:
            exit("the dim %d is not multiply of \
             pool_size %d" % (dim, self.pool_size))

        joint_feature_reshape = joint_feature.view(
            batch_size, num_loc, int(dim / self.pool_size), self.pool_size)

        # N x 100 x 1000 x 1
        iatt_iq_sumpool = torch.sum(joint_feature_reshape, 3)

        iatt_iq_sqrt = (torch.sqrt(self.relu(iatt_iq_sumpool))
                        - torch.sqrt(self.relu(-iatt_iq_sumpool)))

        iatt_iq_sqrt = iatt_iq_sqrt.view(batch_size, -1)  # N x 100000
        iatt_iq_l2 = nn.functional.normalize(iatt_iq_sqrt)
        iatt_iq_l2 = iatt_iq_l2.view(
            batch_size, num_loc, int(dim / self.pool_size))

        if orig_feature_size == 2:
            iatt_iq_l2 = torch.squeeze(iatt_iq_l2, dim=1)

        return iatt_iq_l2


# need to handle two situations,
# first: image (N, K, i_dim), question (N, q_dim);
# second: image (N, i_dim), question (N, q_dim);
class NonLinearElementMultiply(nn.Module):
    def __init__(self, image_feat_dim, ques_emb_dim, **kwargs):
        super(NonLinearElementMultiply, self).__init__()
        self.fa_image = ReLUWithWeightNormFC(image_feat_dim,
                                             kwargs['hidden_dim'])
        self.fa_txt = ReLUWithWeightNormFC(ques_emb_dim, kwargs['hidden_dim'])

        context_dim = kwargs.get('context_dim', None)
        if context_dim is None:
            context_dim = ques_emb_dim

        self.fa_context = ReLUWithWeightNormFC(context_dim,
                                               kwargs['hidden_dim'])
        self.dropout = nn.Dropout(kwargs['dropout'])
        self.out_dim = kwargs['hidden_dim']

    def forward(self, image_feat, question_embedding, context_embedding=None):
        image_fa = self.fa_image(image_feat)
        question_fa = self.fa_txt(question_embedding)

        # TODO: Rewrite in a better way or remove this
        #     history_fa = question_fa.new_ones(size=question_fa.size())
        #     history_fa = torch.autograd.Variable(history_fa)
        #
        #     if question_fa.is_cuda is True:
        #         history_fa = history_fa.cuda()
        # else:

        if len(image_feat.data.shape) == 3:
            num_location = image_feat.data.size(1)
            question_fa_expand = torch.unsqueeze(
                question_fa, 1).expand(-1, num_location, -1)
        else:
            question_fa_expand = question_fa

        joint_feature = image_fa * question_fa_expand

        if context_embedding is not None:
            context_fa = self.fa_context(context_embedding)

            context_text_joint_feaure = context_fa * question_fa_expand
            joint_feature = torch.cat([joint_feature,
                                       context_text_joint_feaure], dim=1)

        joint_feature = self.dropout(joint_feature)

        return joint_feature


class TwoLayerElementMultiply(nn.Module):
    def __init__(self, image_feat_dim, ques_emb_dim, **kwargs):
        super(TwoLayerElementMultiply, self).__init__()

        self.fa_image1 = ReLUWithWeightNormFC(image_feat_dim,
                                              kwargs['hidden_dim'])
        self.fa_image2 = ReLUWithWeightNormFC(
            kwargs['hidden_dim'], kwargs['hidden_dim'])
        self.fa_txt1 = ReLUWithWeightNormFC(ques_emb_dim, kwargs['hidden_dim'])
        self.fa_txt2 = ReLUWithWeightNormFC(
            kwargs['hidden_dim'], kwargs['hidden_dim'])

        self.dropout = nn.Dropout(kwargs['dropout'])

        self.out_dim = kwargs['hidden_dim']

    def forward(self, image_feat, question_embedding):
        image_fa = self.fa_image2(self.fa_image1(image_feat))
        question_fa = self.fa_txt2(self.fa_txt1(question_embedding))

        if len(image_feat.data.shape) == 3:
            num_location = image_feat.data.size(1)
            question_fa_expand = torch.unsqueeze(
                question_fa, 1).expand(-1, num_location, -1)
        else:
            question_fa_expand = question_fa

        joint_feature = image_fa * question_fa_expand
        joint_feature = self.dropout(joint_feature)

        return joint_feature


class TransformLayer(nn.Module):
    def __init__(self, transform_type, in_dim, out_dim, hidden_dim=None):
        super(TransformLayer, self).__init__()

        if transform_type == "linear":
            self.module = LinearTransform(in_dim, out_dim)
        elif transform_type == "conv":
            self.module = ConvTransform(in_dim, out_dim, hidden_dim)
        else:
            raise NotImplementedError(
                "Unknown post combine transform type: %s" % transform_type
            )
        self.out_dim = self.module.out_dim

    def forward(self, *args, **kwargs):
        return self.module(*args, **kwargs)


class LinearTransform(nn.Module):
    def __init__(self, in_dim, out_dim):
        super(LinearTransform, self).__init__()
        self.lc = weight_norm(
            nn.Linear(in_features=in_dim,
                      out_features=out_dim), dim=None)
        self.out_dim = out_dim

    def forward(self, x):
        return self.lc(x)


class ConvTransform(nn.Module):
    def __init__(self, in_dim, out_dim, hidden_dim):
        super(ConvTransform, self).__init__()
        self.conv1 = nn.Conv2d(in_channels=in_dim,
                               out_channels=hidden_dim,
                               kernel_size=1)
        self.conv2 = nn.Conv2d(in_channels=hidden_dim,
                               out_channels=out_dim,
                               kernel_size=1)
        self.out_dim = out_dim

    def forward(self, x):
        if len(x.size()) == 3:  # N x k xdim
            # N x dim x k x 1
            x_reshape = torch.unsqueeze(x.permute(0, 2, 1), 3)
        elif len(x.size()) == 2:  # N x dim
            # N x dim x 1 x 1
            x_reshape = torch.unsqueeze(torch.unsqueeze(x, 2), 3)

        iatt_conv1 = self.conv1(x_reshape)  # N x hidden_dim x * x 1
        iatt_relu = nn.functional.relu(iatt_conv1)
        iatt_conv2 = self.conv2(iatt_relu)  # N x out_dim x * x 1

        if len(x.size()) == 3:
            iatt_conv3 = torch.squeeze(iatt_conv2, 3).permute(0, 2, 1)
        elif len(x.size()) == 2:
            iatt_conv3 = torch.squeeze(torch.squeeze(iatt_conv2, 3), 2)

        return iatt_conv3


class BCNet(nn.Module):
    """
    Simple class for non-linear bilinear connect network
    """
    def __init__(self, v_dim, q_dim, h_dim, h_out,
                 act='ReLU', dropout=[.2, .5], k=3):
        super(BCNet, self).__init__()

        self.c = 32
        self.k = k
        self.v_dim = v_dim
        self.q_dim = q_dim
        self.h_dim = h_dim
        self.h_out = h_out

        self.v_net = FCNet([v_dim, h_dim * self.k], act=act, dropout=dropout[0])
        self.q_net = FCNet([q_dim, h_dim * self.k], act=act, dropout=dropout[0])
        self.dropout = nn.Dropout(dropout[1])

        if k > 1:
            self.p_net = nn.AvgPool1d(self.k, stride=self.k)

        if h_out is None:
            pass

        elif h_out <= self.c:
            self.h_mat = nn.Parameter(torch.Tensor(1, h_out,
                                                   1, h_dim * self.k).normal_())
            self.h_bias = nn.Parameter(torch.Tensor(1, h_out, 1, 1).normal_())
        else:
            self.h_net = weight_norm(nn.Linear(h_dim * self.k, h_out), dim=None)

    def forward(self, v, q):
        if self.h_out is None:
            v_ = self.v_net(v).transpose(1, 2).unsqueeze(3)
            q_ = self.q_net(q).transpose(1, 2).unsqueeze(2)
            d_ = torch.matmul(v_, q_)
            logits = d_.transpose(1, 2).transpose(2, 3)
            return logits

        # broadcast Hadamard product, matrix-matrix production
        # fast computation but memory inefficient
        elif self.h_out <= self.c:
            v_ = self.dropout(self.v_net(v)).unsqueeze(1)
            q_ = self.q_net(q)
            h_ = v_ * self.h_mat
            logits = torch.matmul(h_, q_.unsqueeze(1).transpose(2, 3))
            logits = logits + self.h_bias
            return logits

        # batch outer product, linear projection
        # memory efficient but slow computation
        else:
            v_ = self.dropout(self.v_net(v)).transpose(1, 2).unsqueeze(3)
            q_ = self.q_net(q).transpose(1, 2).unsqueeze(2)
            d_ = torch.matmul(v_, q_)
            logits = self.h_net(d_.transpose(1, 2).transpose(2, 3))
            return logits.transpose(2, 3).transpose(1, 2)

    def forward_with_weights(self, v, q, w):
        v_ = self.v_net(v).transpose(1, 2).unsqueeze(2)
        q_ = self.q_net(q).transpose(1, 2).unsqueeze(3)
        logits = torch.matmul(torch.matmul(v_, w.unsqueeze(1)), q_)
        logits = logits.squeeze(3).squeeze(2)

        if self.k > 1:
            logits = logits.unsqueeze(1)
            logits = self.p_net(logits).squeeze(1) * self.k

        return logits


class FCNet(nn.Module):
    """
    Simple class for non-linear fully connect network
    """
    def __init__(self, dims, act='ReLU', dropout=0):
        super(FCNet, self).__init__()

        layers = []
        for i in range(len(dims)-2):
            in_dim = dims[i]
            out_dim = dims[i+1]

            if dropout > 0:
                layers.append(nn.Dropout(dropout))

            layers.append(weight_norm(nn.Linear(in_dim, out_dim), dim=None))

            if act is not None:
                layers.append(getattr(nn, act)())

        if dropout > 0:
            layers.append(nn.Dropout(dropout))

        layers.append(weight_norm(nn.Linear(dims[-2], dims[-1]), dim=None))

        if act is not None:
            layers.append(getattr(nn, act)())

        self.main = nn.Sequential(*layers)

    def forward(self, x):
        return self.main(x)


class BiAttention(nn.Module):
    def __init__(self, x_dim, y_dim, z_dim, glimpse, dropout=[.2, .5]):
        super(BiAttention, self).__init__()

        self.glimpse = glimpse
        self.logits = weight_norm(BCNet(x_dim,
                                        y_dim,
                                        z_dim,
                                        glimpse,
                                        dropout=dropout,
                                        k=3),
                                  name='h_mat',
                                  dim=None)

    def forward(self, v, q, v_mask=True):
        p, logits = self.forward_all(v, q, v_mask)
        return p, logits

    def forward_all(self, v, q, v_mask=True):
        v_num = v.size(1)
        q_num = q.size(1)
        logits = self.logits(v, q)

        if v_mask:
            v_abs_sum = v.abs().sum(2)
            mask = (v_abs_sum == 0).unsqueeze(1).unsqueeze(3)
            mask = mask.expand(logits.size())
            logits.data.masked_fill_(mask.data, -float('inf'))

        expanded_logits = logits.view(-1, self.glimpse, v_num * q_num)
        p = nn.functional.softmax(expanded_logits, 2)

        return p.view(-1, self.glimpse, v_num, q_num), logits
