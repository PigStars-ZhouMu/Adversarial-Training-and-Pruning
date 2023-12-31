import torch
import torch.nn as nn
import torch.nn.functional as F


class LeNet_adv(nn.Module):
    def __init__(self, w=1, rho=0.001):
        super(LeNet_adv, self).__init__()

        self.w = w
        print('LeNet width multiplier :', w)
        self.conv1 = nn.Conv2d(1, 2 * self.w, 5, 1, 2)
        self.conv2 = nn.Conv2d(2 * self.w, 4 * self.w, 5, 1, 2)
        self.fc1 = nn.Linear(7 * 7 * 4 * self.w, 64 * self.w)
        self.fc2 = nn.Linear(64 * self.w, 10)

    def forward(self, x):
        x = F.relu(self.conv1(x))
        x = F.max_pool2d(x, 2, 2)
        x = F.relu(self.conv2(x))
        x = F.max_pool2d(x, 2, 2)
        x = x.view(-1, 7 * 7 * 4 * self.w)
        x = F.relu(self.fc1(x))
        x = self.fc2(x)
        return F.log_softmax(x, dim=1)
