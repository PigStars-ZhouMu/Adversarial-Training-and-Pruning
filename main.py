import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.autograd import Variable
from torchvision import datasets, transforms
import sys
import os
import numpy as np
from numpy import linalg as LA
import argparse
from config import Config
import admm
from lenet_adv import LeNet_adv

best_adv_acc = 0


def save_checkpoint(config, state, is_best, filename='checkpoint.pth.tar'):
    torch.save(state, filename)


class AttackPGD(nn.Module):
    def __init__(self, basic_model, config):
        super(AttackPGD, self).__init__()
        self.basic_model = basic_model
        self.rand = config.random_start
        self.step_size = config.step_size
        self.epsilon = config.epsilon
        self.num_steps = config.num_steps

    def forward(self, input, target):
        x = input.detach()

        if self.rand:
            x = x + torch.zeros_like(x).uniform_(-self.epsilon, self.epsilon)
        for i in range(self.num_steps):
            x.requires_grad_()
            with torch.enable_grad():
                logits = self.basic_model(x)
                # loss = F.cross_entropy(logits, target, size_average=False) # 在最新版本的pytorch中，size_average被舍弃了
                loss = F.cross_entropy(logits, target, reduction='sum')
            grad = torch.autograd.grad(loss, [x])[0]
            x = x.detach() + self.step_size * torch.sign(grad.detach())
            x = torch.min(torch.max(x, input - self.epsilon), input + self.epsilon)

            x = torch.clamp(x, 0, 1)

        return self.basic_model(input), self.basic_model(x), x


def train(config, ADMM, device, train_loader, optimizer, epoch):
    config.model.train()

    adv_loss = None
    for batch_idx, (data, target) in enumerate(train_loader):
        data, target = data.to(device), target.to(device)

        if config.gpu is not None:
            data = data.cuda(config.gpu, non_blocking=True)
            target = target.cuda(config.gpu, non_blocking=True)

        optimizer.zero_grad()
        nat_output, adv_output, pert_inputs = config.model(data, target)

        nat_loss = F.cross_entropy(nat_output, target)
        adv_loss = F.cross_entropy(adv_output, target)

        if config.admm:
            admm.admm_update(config, ADMM, device, train_loader, optimizer, epoch, data,
                             batch_idx)  # update Z and U
            adv_loss, admm_loss, mixed_loss = admm.append_admm_loss(config, ADMM, adv_loss)  # append admm losss

        if config.admm:
            mixed_loss.backward()
        else:
            adv_loss.backward()

        if config.masked_progressive:
            with torch.no_grad():
                for name, W in config.model.named_parameters():
                    if name in config.zero_masks:
                        W.grad *= config.zero_masks[name]

        if config.masked_retrain:
            with torch.no_grad():
                for name, W in config.model.named_parameters():
                    if name in config.masks:
                        W.grad *= config.masks[name]

        optimizer.step()
        if batch_idx % config.print_freq == 0:
            print("nat_cross_entropy loss: {}  adv_cross_entropy loss : {}".format(nat_loss, adv_loss))
            print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                epoch, batch_idx * len(data), len(train_loader.dataset),
                       100. * batch_idx / len(train_loader), adv_loss.item()))


def test(config, device, test_loader):
    config.model.eval()
    nat_test_loss = 0
    nat_correct = 0
    adv_test_loss = 0
    adv_correct = 0

    with torch.no_grad():
        for data, target in test_loader:
            data, target = data.to(device), target.to(device)

            if config.gpu is not None:
                data = data.cuda(config.gpu, non_blocking=True)
                target = target.cuda(config.gpu, non_blocking=True)
            nat_output, adv_output, pert_inputs = config.model(data, target)

            nat_test_loss += F.nll_loss(nat_output, target, reduction='sum').item()  # sum up batch loss
            nat_pred = nat_output.max(1, keepdim=True)[1]  # get the index of the max log-probability
            nat_correct += nat_pred.eq(target.view_as(nat_pred)).sum().item()

            adv_test_loss += F.nll_loss(adv_output, target, reduction='sum').item()  # sum up batch loss
            adv_pred = adv_output.max(1, keepdim=True)[1]  # get the index of the max log-probability
            adv_correct += adv_pred.eq(target.view_as(adv_pred)).sum().item()

    nat_test_loss /= len(test_loader.dataset)
    adv_test_loss /= len(test_loader.dataset)

    adv_acc = 100. * adv_correct / len(test_loader.dataset)
    nat_acc = 100. * nat_correct / len(test_loader.dataset)
    global best_adv_acc
    if adv_acc > best_adv_acc and not config.admm:
        best_adv_acc = adv_acc
        print('new best adv acc is {}'.format(best_adv_acc))
        print('saving model {}'.format(config.save_model))
        torch.save(config.model.state_dict(), config.save_model)
    print('\nTest set: Average nat_loss: {:.4f}, nat_Accuracy: {}/{} ({:.2f}%)\n'.format(
        nat_test_loss, nat_correct, len(test_loader.dataset),
        100. * nat_correct / len(test_loader.dataset)))

    print('\nTest set: Average adv_loss: {:.4f}, adv Accuracy: {}/{} ({:.2f}%)\n'.format(
        adv_test_loss, adv_correct, len(test_loader.dataset),
        100. * adv_correct / len(test_loader.dataset)))


def main():
    # 设置命令行参数
    parser = argparse.ArgumentParser(description='PyTorch MNIST')
    parser.add_argument('--config_file', type=str, default='', help="config file")
    parser.add_argument('--stage', type=str, default='', help="select the pruning stage")
    args = parser.parse_args()

    use_cuda = True
    config = Config(args)
    torch.manual_seed(config.random_seed)

    device = torch.device("cuda" if use_cuda else "cpu")
    kwargs = {'num_workers': 1, 'pin_memory': True} if use_cuda else {}

    train_loader = torch.utils.data.DataLoader(
        datasets.MNIST('./data', train=True, download=True,
                       transform=transforms.Compose([
                           transforms.ToTensor()
                       ])),
        batch_size=64, shuffle=True, **kwargs)
    test_loader = torch.utils.data.DataLoader(
        datasets.MNIST('./data', train=False, transform=transforms.Compose([
            transforms.ToTensor()
        ])),
        batch_size=1000, shuffle=True, **kwargs)

    model = LeNet_adv(w=config.width_multiplier).to(device)

    for name, W in model.named_parameters():
        if 'conv' in name and 'bias' not in name:
            print('initialization uniform')
            W.data = torch.nn.init.xavier_uniform_(W.data)

    model = AttackPGD(model, config)
    config.model = model

    if config.load_model:
        # unlike resume, load model does not care optimizer status or start_epoch
        print('==> Loading from {}'.format(config.load_model))
        config.model.load_state_dict(torch.load(config.load_model, map_location=lambda storage, loc: storage))

    torch.cuda.set_device(config.gpu)
    config.model.cuda(config.gpu)
    test(config, device, test_loader)
    ADMM = None

    config.prepare_pruning()

    if config.admm:
        ADMM = admm.ADMM(config)

    optimizer = None
    if (config.optimizer == 'sgd'):
        optimizer = torch.optim.SGD(config.model.parameters(), config.lr,
                                    momentum=0.9,
                                    weight_decay=1e-6)

    elif (config.optimizer == 'adam'):
        optimizer = torch.optim.Adam(config.model.parameters(), config.lr)

    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=config.epochs * len(train_loader), eta_min=4e-08)

    if config.masked_retrain:
        # make sure small weights are pruned and confirm the acc
        print("<============masking both weights and gradients for retrain")
        admm.masking(config)

        print("<============testing sparsity before retrain")
        admm.test_sparsity(config)
        test(config, device, test_loader)
    if config.masked_progressive:
        admm.zero_masking(config)

    for epoch in range(0, config.epochs + 1):

        if config.admm:
            admm.admm_adjust_learning_rate(optimizer, epoch, config)
        else:
            if config.lr_scheduler == 'cosine':
                scheduler.step()
            elif config.lr_scheduler == 'sgd':
                if epoch == 20:
                    config.lr /= 10
                    for param_group in optimizer.param_groups:
                        param_group['lr'] = config.lr
            else:
                pass  # it uses adam

        train(config, ADMM, device, train_loader, optimizer, epoch)
        test(config, device, test_loader)

    admm.test_sparsity(config)
    test(config, device, test_loader)
    if config.save_model and config.admm:
        print('saving model {}'.format(config.save_model))
        torch.save(config.model.state_dict(), config.save_model)


if __name__ == "__main__":
    main()

# python -u main.py --config_file config.yaml --stage pretrain
# python -u main.py --config_file config.yaml --stage admm
# python -u main.py --config_file config.yaml --stage retrain