import json
import sys
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F


def get_state_dict(file):
    try:
        pretrain_state_dict = torch.load(file)
    except AssertionError:
        pretrain_state_dict = torch.load(file, map_location=lambda storage, location: storage)
    return pretrain_state_dict


def get_flat_params_from(model):
    params = []
    for param in model.parameters():
        params.append(param.data.view(-1))

    flat_params = torch.cat(params)
    return flat_params


def set_flat_params_to(model, flat_params):
    prev_ind = 0
    for param in model.parameters():
        flat_size = int(np.prod(list(param.size())))
        param.data.copy_(
            flat_params[prev_ind:prev_ind + flat_size].view(param.size()))
        prev_ind += flat_size


class RunningAverage():
    """A simple class that maintains the running average of a quantity

    Example:
    ```
    loss_avg = RunningAverage()
    loss_avg.update(2)
    loss_avg.update(4)
    loss_avg() = 3
    ```
    """

    def __init__(self):
        self.steps = 0
        self.total = 0

    def update(self, val):
        self.total += val
        self.steps += 1

    def value(self):
        return self.total / float(self.steps)


def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, dim=1, largest=True, sorted=True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
# correct_k = correct[:k].view(-1).float().sum(0)
        correct_k = torch.reshape(correct[:k], (-1,)).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res


class KL_Loss(nn.Module):
    def __init__(self, temperature=1):
        super(KL_Loss, self).__init__()
        self.T = temperature

    def forward(self, output_batch, teacher_outputs):
        # output_batch  -> B X num_classes
        # teacher_outputs -> B X num_classes

        # loss_2 = -torch.sum(torch.sum(torch.mul(F.log_softmax(teacher_outputs,dim=1), F.softmax(teacher_outputs,dim=1)+10**(-7))))/teacher_outputs.size(0)
        # print('loss H:',loss_2)

        output_batch = F.log_softmax(output_batch / self.T, dim=1)
        teacher_outputs = F.softmax(teacher_outputs / self.T, dim=1) + 10 ** (-7)

        loss = self.T * self.T * nn.KLDivLoss(reduction='batchmean')(output_batch, teacher_outputs)

        # Same result KL-loss implementation
        # loss = T * T * torch.sum(torch.sum(torch.mul(teacher_outputs, torch.log(teacher_outputs) - output_batch)))/teacher_outputs.size(0)
        return loss

def IID_loss(x_out, x_tf_out, lamb=1.0, EPS=sys.float_info.epsilon):
    # has had softmax applied
    _, k = x_out.size()
    p_i_j = compute_joint(x_out, x_tf_out)
    assert (p_i_j.size() == (k, k))

    p_i = p_i_j.sum(dim=1).view(k, 1).expand(k, k).clone()
    p_j = p_i_j.sum(dim=0).view(1, k).expand(k, k).clone()  # but should be same, symmetric

    # avoid NaN losses. Effect will get cancelled out by p_i_j tiny anyway
    p_i_j[(p_i_j < EPS).data] = EPS
    p_j[(p_j < EPS).data] = EPS
    p_i[(p_i < EPS).data] = EPS

    loss = - p_i_j * (torch.log(p_i_j) \
                      - lamb * torch.log(p_j) \
                      - lamb * torch.log(p_i))

    loss = loss.sum()
    return loss


def compute_joint(x_out, x_tf_out):
    # produces variable that requires grad (since args require grad)
    bn, k = x_out.size()
    assert (x_tf_out.size(0) == bn and x_tf_out.size(1) == k)

    p_i_j = x_out.unsqueeze(2) * x_tf_out.unsqueeze(1)  # bn, k, k
    p_i_j = p_i_j.sum(dim=0)  # k, k
    p_i_j = (p_i_j + p_i_j.t()) / 2.  # symmetrise
    p_i_j = p_i_j / p_i_j.sum()  # normalise

    return p_i_j

class MutualInformationLoss(nn.Module):
    def __init__(self, temperature=1):
        super(MutualInformationLoss, self).__init__()
        self.T = temperature

    def _compute_joint(self, logits, logits_aug):
        b, dim = logits.size()
        assert(logits_aug.size(0)==b and logits_aug.size(1)==dim)

        pij = logits.unsqueeze(2) * logits_aug.unsqueeze(1) # b, c, c
        pij = pij.sum(dim=0) # c, c
        pij = (pij + pij.t()) / 2.
        pij = pij / pij.sum()
        return pij

    def forward(self, output_batch, output_batch_aug, lamb=1.0, EPS=sys.float_info.epsilon):
        # output_batch : b x c
        # output_batch_aug : b x c
        b, c = output_batch.size()

        logits = F.log_softmax(output_batch / self.T, dim=1)
        logits_aug = F.log_softmax(output_batch_aug / self.T, dim=1)
        pij = self._compute_joint(logits, logits_aug)

        assert(pij.size() == (c, c))

        pi = pij.sum(dim=1).view(c, 1).expand(c, c)
        pj = pij.sum(dim=0).view(1, c).expand(c, c)

        pij[(pij < EPS).data] = EPS
        pj[(pj < EPS).data] = EPS
        pi[(pi < EPS).data] = EPS

        loss = - pij * (torch.log(pij) \
              - lamb * torch.log(pj) \
              - lamb * torch.log(pi))

        loss = loss.sum()

        return loss

class CE_Loss(nn.Module):
    def __init__(self, temperature=1):
        super(CE_Loss, self).__init__()
        self.T = temperature

    def forward(self, output_batch, teacher_outputs):
        # output_batch      -> B X num_classes
        # teacher_outputs   -> B X num_classes

        output_batch = F.log_softmax(output_batch / self.T, dim=1)
        teacher_outputs = F.softmax(teacher_outputs / self.T, dim=1)

        # Same result CE-loss implementation torch.sum -> sum of all element
        loss = -self.T * self.T * torch.sum(torch.mul(output_batch, teacher_outputs)) / teacher_outputs.size(0)

        return loss


def save_dict_to_json(d, json_path):
    """Saves dict of floats in json file

    Args:
        d: (dict) of float-castable values (np.float, int, float, etc.)
        json_path: (string) path to json file
    """
    with open(json_path, 'w') as f:
        # We need to convert the values to float for json (it doesn't accept np.array, np.float, )
        d = {k: v for k, v in d.items()}
        json.dump(d, f, indent=4)


# Filter out batch norm parameters and remove them from weight decay - gets us higher accuracy 93.2 -> 93.48
# https://arxiv.org/pdf/1807.11205.pdf
def bnwd_optim_params(model, model_params, master_params):
    bn_params, remaining_params = split_bn_params(model, model_params, master_params)
    return [{'params': bn_params, 'weight_decay': 0}, {'params': remaining_params}]


def split_bn_params(model, model_params, master_params):
    def get_bn_params(module):
        if isinstance(module, torch.nn.modules.batchnorm._BatchNorm): return module.parameters()
        accum = set()
        for child in module.children(): [accum.add(p) for p in get_bn_params(child)]
        return accum

    mod_bn_params = get_bn_params(model)
    zipped_params = list(zip(model_params, master_params))

    mas_bn_params = [p_mast for p_mod, p_mast in zipped_params if p_mod in mod_bn_params]
    mas_rem_params = [p_mast for p_mod, p_mast in zipped_params if p_mod not in mod_bn_params]
    return mas_bn_params, mas_rem_params
