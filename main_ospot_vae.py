import random

random.seed(1)
import numpy as np

np.random.seed(1)
import argparse
from ospot_vae_model.vae import VariationalAutoEncoder
from lib.criterion import VAECriterion, KLDiscCriterion, KLNormCriterion, ClsCriterion
from lib.utils.avgmeter import AverageMeter
from lib.utils.mixup import mixup_vae_data, mixup_raw_labeled_data
from lib.dataloader import cifar10_dataset, get_cifar10_ssl_sampler, cifar100_dataset, get_cifar100_ssl_sampler
import os
from os import path
import time
import shutil
import ast
from itertools import cycle
from collections import defaultdict
import re
from lib.utils.utils import get_score_label_array_from_dict
from sklearn.metrics import roc_auc_score
import math


def arg_as_list(s):
    v = ast.literal_eval(s)
    if type(v) is not list:
        raise argparse.ArgumentTypeError("Argument \"%s\" is not a list" % (s))
    return v


parser = argparse.ArgumentParser(description='Pytorch Training Semi-Supervised VAE for Cifar10,Cifar100,SVHN Dataset')
# Dataset Parameters
parser.add_argument('-bp', '--base_path', default=".")
parser.add_argument('--dataset', default="Cifar10", type=str, help="name of dataset used")
parser.add_argument('-is', "--image-size", default=[32, 32], type=arg_as_list,
                    metavar='Image Size List', help='the size of h * w for image')
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('-b', '--batch-size', default=384, type=int,
                    metavar='N', help='mini-batch size (default: 256)')
# SSL VAE Train Strategy Parameters
parser.add_argument('-t', '--train-time', default=1, type=int,
                    metavar='N', help='the x-th time of training')
parser.add_argument('--epochs', default=600, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('--dp', '--data-parallel', action='store_false', help='Use Data Parallel')
parser.add_argument('--print-freq', '-p', default=3, type=int,
                    metavar='N', help='print frequency (default: 10)')
parser.add_argument('--reconstruct-freq', '-rf', default=20, type=int,
                    metavar='N', help='reconstruct frequency (default: 1)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('--resume-arg', action='store_false', help='if we not resume the argument')
parser.add_argument('--annotated-ratio', default=0.1, type=float, help='The ratio for semi-supervised annotation')
# Deep Learning Model Parameters
parser.add_argument('--net-name', default="wideresnet-28-10", type=str, help="the name for network to use")
parser.add_argument('--temperature', default=0.67, type=float,
                    help='centeralization parameter')
parser.add_argument('-dr', '--drop-rate', default=0, type=float, help='drop rate for the network')
parser.add_argument("--br", "--bce-reconstruction", action='store_true', help='Do BCE Reconstruction')
parser.add_argument("-s", "--x-sigma", default=1, type=float,
                    help="The standard variance for reconstructed images, work as regularization")
parser.add_argument('--ldc', "--latent-dim-continuous", default=128, type=int,
                    metavar='Latent Dim For Continuous Variable',
                    help='feature dimension in latent space for continuous variable')
parser.add_argument('--cmi', "--continuous-mutual-info", default=200, type=float,
                    help='The mutual information bounding between x and the continuous variable z')
parser.add_argument('--dmi', "--discrete-mutual-info", default=2.3, type=float,
                    help='The mutual information bounding between x and the discrete variable z')
# Loss Function Parameters
parser.add_argument('--kbmc', '--kl-beta-max-continuous', default=1e-3, type=float, metavar='KL Beta',
                    help='the epoch to linear adjust kl beta')
parser.add_argument('--kbmd', '--kl-beta-max-discrete', default=1e-3, type=float, metavar='KL Beta',
                    help='the epoch to linear adjust kl beta')
parser.add_argument('--akb', '--adjust-kl-beta-epoch', default=200, type=int, metavar='KL Beta',
                    help='the max epoch to adjust kl beta')
parser.add_argument('--ewm', '--elbo-weight-max', default=1e-3, type=float, metavar='weight for elbo loss part')
parser.add_argument('--aew', '--adjust-elbo-weight', default=400, type=int,
                    metavar="the epoch to adjust elbo weight to max")
parser.add_argument('--wrd', default=1, type=float,
                    help="the max weight for the optimal transport estimation of discrete variable c")
parser.add_argument('--wmf', '--weight-modify-factor', default=0.4, type=float,
                    help="weight  will get wrz at amf * epochs")
parser.add_argument('--pwm', '--posterior-weight-max', default=1, type=float,
                    help="the max value for posterior weight")
parser.add_argument('--apw', '--adjust-posterior-weight', default=200, type=float,
                    help="adjust posterior weight")
# Optimizer Parameters
parser.add_argument('-on', '--optimizer-name', default="SGD", type=str, metavar="Optimizer Name",
                    help="The name for the optimizer we used")
parser.add_argument('--lr', '--learning-rate', default=1e-1, type=float,
                    metavar='LR', help='initial learning rate')
parser.add_argument('-b1', '--beta1', default=0.9, type=float, metavar='Beta1 In ADAM and SGD',
                    help='beta1 for adam as well as momentum for SGD')
parser.add_argument('-ad', "--adjust-lr", default=[400, 500, 550], type=arg_as_list,
                    help="The milestone list for adjust learning rate")
parser.add_argument('--wd', '--weight-decay', default=5e-4, type=float)
# Optimizer Transport Estimation Parameters
parser.add_argument('--mas', "--mixup-alpha-supervised", default=0.1, type=float,
                    help="the mixup alpha for labeled data")
parser.add_argument('--mau', "--mixup-alpha-unsupervised", default=2, type=float,
                    help="the mixup alpha for unlabeled data")
# GPU Parameters
parser.add_argument("--gpu", default="0,1", type=str, metavar='GPU plans to use', help='The GPU id plans to use')
args = parser.parse_args()
os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu

import torch
from torch.utils.data import DataLoader
from torchvision import utils
from torch.utils.tensorboard import SummaryWriter
from torch.optim.lr_scheduler import MultiStepLR
import torch.nn.functional as F

torch.manual_seed(1)
torch.cuda.manual_seed(1)


def main(args=args):
    if args.dataset == "Cifar10":
        dataset_base_path = path.join(args.base_path, "dataset", "cifar")
        train_dataset = cifar10_dataset(dataset_base_path)
        test_dataset = cifar10_dataset(dataset_base_path, train_flag=False)
        sampler_valid, sampler_train_l, sampler_train_u = get_cifar10_ssl_sampler(
            torch.tensor(train_dataset.targets, dtype=torch.int32), 500, round(4000 * args.annotated_ratio), 10)
        test_dloader = DataLoader(test_dataset, batch_size=args.batch_size, num_workers=args.workers, pin_memory=True)
        valid_dloader = DataLoader(train_dataset, batch_size=args.batch_size, num_workers=args.workers, pin_memory=True,
                                   sampler=sampler_valid)
        train_dloader_l = DataLoader(train_dataset, batch_size=args.batch_size, num_workers=args.workers,
                                     pin_memory=True,
                                     sampler=sampler_train_l)
        train_dloader_u = DataLoader(train_dataset, batch_size=args.batch_size, num_workers=args.workers,
                                     pin_memory=True,
                                     sampler=sampler_train_u)
        input_channels = 3
        small_input = True
        discrete_latent_dim = 10
        elbo_criterion = VAECriterion(discrete_dim=discrete_latent_dim, x_sigma=args.x_sigma,
                                      bce_reconstruction=args.br).cuda()
        cls_criterion = ClsCriterion()
    elif args.dataset == "Cifar100":
        dataset_base_path = path.join(args.base_path, "dataset", "cifar")
        train_dataset = cifar100_dataset(dataset_base_path)
        test_dataset = cifar100_dataset(dataset_base_path, train_flag=False)
        sampler_valid, sampler_train_l, sampler_train_u = get_cifar100_ssl_sampler(
            torch.tensor(train_dataset.targets, dtype=torch.int32), 50, round(400 * args.annotated_ratio), 100)
        test_dloader = DataLoader(test_dataset, batch_size=args.batch_size, num_workers=args.workers, pin_memory=True)
        valid_dloader = DataLoader(train_dataset, batch_size=args.batch_size, num_workers=args.workers, pin_memory=True,
                                   sampler=sampler_valid)
        train_dloader_l = DataLoader(train_dataset, batch_size=args.batch_size, num_workers=args.workers,
                                     pin_memory=True,
                                     sampler=sampler_train_l)
        train_dloader_u = DataLoader(train_dataset, batch_size=args.batch_size, num_workers=args.workers,
                                     pin_memory=True,
                                     sampler=sampler_train_u)
        input_channels = 3
        small_input = True
        discrete_latent_dim = 100
        elbo_criterion = VAECriterion(discrete_dim=discrete_latent_dim, x_sigma=args.x_sigma,
                                      bce_reconstruction=args.br).cuda()
        cls_criterion = ClsCriterion()
    else:
        raise NotImplementedError("Dataset {} not implemented".format(args.dataset))
    model = VariationalAutoEncoder(encoder_name=args.net_name, num_input_channels=input_channels,
                                   drop_rate=args.drop_rate, img_size=tuple(args.image_size), data_parallel=args.dp,
                                   continuous_latent_dim=args.ldc, disc_latent_dim=discrete_latent_dim,
                                   sample_temperature=args.temperature, small_input=small_input)
    model = model.cuda()
    kl_disc_criterion = KLDiscCriterion().cuda()
    kl_norm_criterion = KLNormCriterion().cuda()

    print("Begin the {} Time's Training Semi-Supervised VAE, Dataset {}".format(args.train_time, args.dataset))
    if args.optimizer_name == "SGD":
        optimizer = torch.optim.SGD(model.parameters(), lr=args.lr, momentum=args.beta1, weight_decay=args.wd)
    elif args.optimizer_name == "Adam":
        optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, betas=(args.beta1, 0.999), weight_decay=args.wd)
    else:
        raise NotImplementedError("Optimizer {} Not Implemented".format(args.optimizer_name))
    scheduler = MultiStepLR(optimizer, milestones=args.adjust_lr)
    writer_log_dir = "{}/{}-OSPOT-VAE/runs/train_time:{}".format(args.base_path, args.dataset,
                                                               args.train_time)
    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args = checkpoint['args']
            args.start_epoch = checkpoint['epoch']
            model.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch']))
        else:
            raise FileNotFoundError("Checkpoint Resume File {} Not Found".format(args.resume))
    else:
        if os.path.exists(writer_log_dir):
            flag = input("vae_train_time:{} will be removed, input yes to continue:".format(
                args.train_time))
            if flag == "yes":
                shutil.rmtree(writer_log_dir, ignore_errors=True)
    writer = SummaryWriter(log_dir=writer_log_dir)
    best_valid_acc = 10
    for epoch in range(args.start_epoch, args.epochs):
        if epoch == 0:
            # do warm up
            modify_lr_rate(opt=optimizer, lr=args.lr * 0.2)
        train(train_dloader_u, train_dloader_l, model=model, elbo_criterion=elbo_criterion, cls_criterion=cls_criterion,
              optimizer=optimizer, epoch=epoch,
              writer=writer, discrete_latent_dim=discrete_latent_dim, kl_norm_criterion=kl_norm_criterion,
              kl_disc_criterion=kl_disc_criterion)
        elbo_valid_loss, *_ = valid(valid_dloader, model=model, elbo_criterion=elbo_criterion,
                                    cls_criterion=cls_criterion, epoch=epoch,
                                    writer=writer, discrete_latent_dim=discrete_latent_dim)
        if test_dloader is not None:
            test(test_dloader, model=model, elbo_criterion=elbo_criterion, cls_criterion=cls_criterion, epoch=epoch,
                 writer=writer, discrete_latent_dim=discrete_latent_dim)
        """
        Here we define the best point as the minimum average epoch loss
        """
        save_checkpoint({
            'epoch': epoch + 1,
            'args': args,
            "state_dict": model.state_dict(),
            'optimizer': optimizer.state_dict(),
        })
        if elbo_valid_loss < best_valid_acc:
            best_valid_acc = elbo_valid_loss
            if epoch >= args.adjust_lr[-1]:
                save_checkpoint({
                    'epoch': epoch + 1,
                    'args': args,
                    "state_dict": model.state_dict(),
                    'optimizer': optimizer.state_dict()
                }, best_predict=True)
        scheduler.step(epoch)
        if epoch == 0:
            modify_lr_rate(opt=optimizer, lr=args.lr)
        if args.dataset == "Cifar10":
            if epoch == args.adjust_lr[0]:
                args.ewm = args.ewm * 5


def train(train_dloader_u, train_dloader_l, model, elbo_criterion, cls_criterion, optimizer, epoch, writer,
          discrete_latent_dim, kl_norm_criterion=None, kl_disc_criterion=None):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    # train_dloader_u part
    reconstruct_losses_u = AverageMeter()
    continuous_prior_kl_losses_u = AverageMeter()
    discrete_prior_kl_losses_u = AverageMeter()
    elbo_losses_u = AverageMeter()
    mse_losses_u = AverageMeter()
    continuous_posterior_kl_losses_u = AverageMeter()
    discrete_posterior_kl_losses_u = AverageMeter()
    # train_dloader_l part
    reconstruct_losses_l = AverageMeter()
    continuous_prior_kl_losses_l = AverageMeter()
    discrete_prior_kl_losses_l = AverageMeter()
    elbo_losses_l = AverageMeter()
    mse_losses_l = AverageMeter()
    continuous_posterior_kl_losses_l = AverageMeter()
    discrete_posterior_kl_losses_l = AverageMeter()
    model.train()
    end = time.time()
    optimizer.zero_grad()
    # mutual information
    cmi = alpha_schedule(epoch, args.akb, args.cmi, strategy="exp")
    dmi = alpha_schedule(epoch, args.akb, args.dmi, strategy="exp")
    # elbo part weight
    ew = alpha_schedule(epoch, args.aew, args.ewm)
    # mixup parameters
    kl_beta_c = alpha_schedule(epoch, args.akb, args.kbmc)
    kl_beta_d = alpha_schedule(epoch, args.akb, args.kbmd)
    pwm = alpha_schedule(epoch, args.apw, args.pwm)
    # unsupervised cls weight
    ucw = alpha_schedule(epoch, round(args.wmf * args.epochs), args.wrd)
    for i, ((image_l, label_l), (image_u, _)) in enumerate(zip(cycle(train_dloader_l), train_dloader_u)):
        if image_l.size(0) != image_u.size(0):
            batch_size = min(image_l.size(0), image_u.size(0))
            image_l = image_l[0:batch_size]
            label_l = label_l[0:batch_size]
            image_u = image_u[0:batch_size]
        else:
            batch_size = image_l.size(0)
        data_time.update(time.time() - end)
        # for the labeled part, do classification and mixup
        image_l = image_l.float().cuda()
        label_l = label_l.long().cuda()
        label_onehot_l = torch.zeros(batch_size, discrete_latent_dim).cuda().scatter_(1, label_l.view(-1, 1), 1)
        reconstruction_l, norm_mean_l, norm_log_sigma_l, disc_log_alpha_l = model(image_l, disc_label=label_l)
        reconstruct_loss_l, continuous_prior_kl_loss_l, disc_prior_kl_loss_l = elbo_criterion(image_l, reconstruction_l,
                                                                                              norm_mean_l,
                                                                                              norm_log_sigma_l,
                                                                                              disc_log_alpha_l)
        prior_kl_loss_l = kl_beta_c * torch.abs(continuous_prior_kl_loss_l - cmi) + kl_beta_d * torch.abs(
            disc_prior_kl_loss_l - dmi)
        elbo_loss_l = reconstruct_loss_l + prior_kl_loss_l
        reconstruct_losses_l.update(float(reconstruct_loss_l.detach().item()), batch_size)
        continuous_prior_kl_losses_l.update(float(continuous_prior_kl_loss_l.detach().item()), batch_size)
        discrete_prior_kl_losses_l.update(float(disc_prior_kl_loss_l.detach().item()), batch_size)
        # do optimal transport estimation
        with torch.no_grad():
            mixed_image_l, mixed_z_mean_l, mixed_z_sigma_l, mixed_disc_alpha_l, label_mixup_l, lam_l = \
                mixup_vae_data(
                    image_l,
                    norm_mean_l,
                    norm_log_sigma_l,
                    disc_log_alpha_l,
                    alpha=args.mas,
                    disc_label=label_l)
            label_mixup_onehot_l = torch.zeros(batch_size, discrete_latent_dim).cuda().scatter_(1,
                                                                                                label_mixup_l.view(
                                                                                                    -1,
                                                                                                    1),
                                                                                                1)
        mixed_reconstruction_l, mixed_norm_mean_l, mixed_norm_log_sigma_l, mixed_disc_log_alpha_l, *_ = model(
            mixed_image_l, mixup=True,
            disc_label=label_l,
            disc_label_mixup=label_mixup_l,
            mixup_lam=lam_l)
        disc_posterior_kl_loss_l = lam_l * cls_criterion(mixed_disc_log_alpha_l, label_onehot_l) + (
                1 - lam_l) * cls_criterion(
            mixed_disc_log_alpha_l, label_mixup_onehot_l)
        continuous_posterior_kl_loss_l = (F.mse_loss(mixed_norm_mean_l, mixed_z_mean_l, reduction="sum") + \
                                          F.mse_loss(torch.exp(mixed_norm_log_sigma_l), mixed_z_sigma_l,
                                                     reduction="sum")) / batch_size
        elbo_loss_l = elbo_loss_l + kl_beta_c * pwm * continuous_posterior_kl_loss_l
        elbo_losses_l.update(float(elbo_loss_l.detach().item()), batch_size)
        continuous_posterior_kl_losses_l.update(float(continuous_posterior_kl_loss_l.detach().item()), batch_size)
        discrete_posterior_kl_losses_l.update(float(disc_posterior_kl_loss_l.detach().item()), batch_size)
        if args.br:
            mse_loss_l = F.mse_loss(torch.sigmoid(reconstruction_l.detach()), image_l.detach(), reduction="sum") / (
                    2 * batch_size * (args.x_sigma ** 2))
            mse_losses_l.update(float(mse_loss_l), batch_size)
        loss_supervised = ew * elbo_loss_l + disc_posterior_kl_loss_l
        loss_supervised.backward()

        # for the unlabeled part, do classification and mixup
        image_u = image_u.float().cuda()
        reconstruction_u, norm_mean_u, norm_log_sigma_u, disc_log_alpha_u = model(image_u)
        reconstruct_loss_u, continuous_prior_kl_loss_u, disc_prior_kl_loss_u = elbo_criterion(image_u, reconstruction_u,
                                                                                              norm_mean_u,
                                                                                              norm_log_sigma_u,
                                                                                              disc_log_alpha_u)
        prior_kl_loss_u = kl_beta_c * torch.abs(continuous_prior_kl_loss_u - cmi) + kl_beta_d * torch.abs(
            disc_prior_kl_loss_u - dmi)
        elbo_loss_u = reconstruct_loss_u + prior_kl_loss_u
        reconstruct_losses_u.update(float(reconstruct_loss_u.detach().item()), batch_size)
        continuous_prior_kl_losses_u.update(float(continuous_prior_kl_loss_u.detach().item()), batch_size)
        discrete_prior_kl_losses_u.update(float(disc_prior_kl_loss_u.detach().item()), batch_size)
        # do mixup part
        with torch.no_grad():
            mixed_image_u, mixed_z_mean_u, mixed_z_sigma_u, mixed_disc_alpha_u, lam_u = \
                mixup_vae_data(
                    image_u,
                    norm_mean_u,
                    norm_log_sigma_u,
                    disc_log_alpha_u,
                    alpha=args.mau)
        mixed_reconstruction_u, mixed_norm_mean_u, mixed_norm_log_sigma_u, mixed_disc_log_alpha_u, *_ = model(
            mixed_image_u)
        disc_posterior_kl_loss_u = cls_criterion(mixed_disc_log_alpha_u, mixed_disc_alpha_u)
        continuous_posterior_kl_loss_u = (F.mse_loss(mixed_norm_mean_u, mixed_z_mean_u, reduction="sum") + \
                                          F.mse_loss(torch.exp(mixed_norm_log_sigma_u), mixed_z_sigma_u,
                                                     reduction="sum")) / batch_size
        elbo_loss_u = elbo_loss_u + kl_beta_c * pwm * continuous_posterior_kl_loss_u
        loss_unsupervised = ew * elbo_loss_u + ucw * disc_posterior_kl_loss_u
        elbo_losses_u.update(float(elbo_loss_u.detach().item()), batch_size)
        continuous_posterior_kl_losses_u.update(float(continuous_posterior_kl_loss_u.detach().item()), batch_size)
        discrete_posterior_kl_losses_u.update(float(disc_posterior_kl_loss_u.detach().item()), batch_size)
        loss_unsupervised.backward()
        if args.br:
            mse_loss_u = F.mse_loss(torch.sigmoid(reconstruction_u.detach()), image_u.detach(), reduction="sum") / (
                    2 * batch_size * (args.x_sigma ** 2))
            mse_losses_u.update(float(mse_loss_u), batch_size)
        optimizer.step()
        optimizer.zero_grad()
        batch_time.update(time.time() - end)
        end = time.time()
        if i % args.print_freq == 0:
            train_text = 'Epoch: [{0}][{1}/{2}]\t' \
                         'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t' \
                         'Data {data_time.val:.3f} ({data_time.avg:.3f})\t' \
                         'Cls Loss Labeled {cls_loss_l.val:.4f} ({cls_loss_l.avg:.4f})\t' \
                         'Cls Loss Unlabeled {cls_loss_u.val:.4f} ({cls_loss_u.avg:.4f})\t' \
                         'Continuous Prior KL Loss Labeled {cpk_loss_l.val:.4f} ({cpk_loss_l.avg:.4f})\t' \
                         'Continuous Prior KL Loss Unlabeled {cpk_loss_u.val:.4f} ({cpk_loss_u.avg:.4f})\t'.format(
                epoch, i + 1, len(train_dloader_u), batch_time=batch_time, data_time=data_time,
                cls_loss_l=discrete_posterior_kl_losses_l, cls_loss_u=discrete_posterior_kl_losses_u,
                cpk_loss_l=continuous_prior_kl_losses_l, cpk_loss_u=continuous_prior_kl_losses_u)
            print(train_text)
    # record unlabeled part loss
    writer.add_scalar(tag="Train/ELBO_U", scalar_value=elbo_losses_u.avg, global_step=epoch + 1)
    writer.add_scalar(tag="Train/Reconstrut_U", scalar_value=reconstruct_losses_u.avg,
                      global_step=epoch + 1)
    writer.add_scalar(tag="Train/Continuous_Prior_KL_U", scalar_value=continuous_prior_kl_losses_u.avg,
                      global_step=epoch + 1)
    writer.add_scalar(tag="Train/Continuous_Posterior_KL_U", scalar_value=continuous_posterior_kl_losses_u.avg,
                      global_step=epoch + 1)
    writer.add_scalar(tag="Train/Discrete_Prior_KL_U", scalar_value=discrete_prior_kl_losses_u.avg,
                      global_step=epoch + 1)
    writer.add_scalar(tag="Train/Discrete_Posterior_KL_U", scalar_value=discrete_posterior_kl_losses_u.avg,
                      global_step=epoch + 1)
    if args.br:
        writer.add_scalar(tag="Train/MSE_U", scalar_value=mse_losses_u.avg, global_step=epoch + 1)
    # record labeled part loss
    writer.add_scalar(tag="Train/ELBO_L", scalar_value=elbo_losses_l.avg, global_step=epoch + 1)
    writer.add_scalar(tag="Train/Reconstruct_L", scalar_value=reconstruct_losses_l.avg,
                      global_step=epoch + 1)
    writer.add_scalar(tag="Train/Continuous_Prior_KL_L", scalar_value=continuous_prior_kl_losses_l.avg,
                      global_step=epoch + 1)
    writer.add_scalar(tag="Train/Continuous_Posterior_KL_L", scalar_value=continuous_posterior_kl_losses_l.avg,
                      global_step=epoch + 1)
    writer.add_scalar(tag="Train/Discrete_Prior_KL_L", scalar_value=discrete_prior_kl_losses_l.avg,
                      global_step=epoch + 1)
    writer.add_scalar(tag="Train/Discrete_Posterior_KL_L", scalar_value=discrete_posterior_kl_losses_l.avg,
                      global_step=epoch + 1)
    if args.br:
        writer.add_scalar(tag="Train/MSE_L", scalar_value=mse_losses_l.avg, global_step=epoch + 1)
    # after several epoch training, we add the image and reconstructed image into the image board, we just use 16 images
    if epoch % args.reconstruct_freq == 0:
        with torch.no_grad():
            image = utils.make_grid(image_u[:4, ...], nrow=2)
            reconstruct_image = utils.make_grid(torch.sigmoid(reconstruction_u[:4, ...]), nrow=2)
        writer.add_image(tag="Train/Raw_Image", img_tensor=image, global_step=epoch + 1)
        writer.add_image(tag="Train/Reconstruct_Image", img_tensor=reconstruct_image, global_step=epoch + 1)
    return discrete_posterior_kl_losses_u.avg, discrete_posterior_kl_losses_l.avg


def save_checkpoint(state, filename='checkpoint.pth.tar', best_predict=False):
    """
    :param state: a dict including:{
                'epoch': epoch + 1,
                'args': args,
                "state_dict": ospot_vae_model.state_dict(),
                'optimizer': optimizer.state_dict(),
        }
    :param filename: the filename for store
    :param best_predict: the best predict flag
    :return:
    """
    filefolder = '{}/{}-OSPOT-VAE/parameter/train_time_{}'.format(args.base_path, args.dataset,
                                                                state["args"].train_time)
    if not path.exists(filefolder):
        os.makedirs(filefolder)
    if best_predict:
        filename = 'best.pth.tar'
        torch.save(state, path.join(filefolder, filename))
    else:
        torch.save(state, path.join(filefolder, filename))


def valid(valid_dloader, model, elbo_criterion, cls_criterion, epoch, writer, discrete_latent_dim):
    reconstruct_losses = AverageMeter()
    continuous_kl_losses = AverageMeter()
    discrete_kl_losses = AverageMeter()
    mse_losses = AverageMeter()
    cls_losses = AverageMeter()
    model.eval()
    all_score = []
    all_label = []

    for i, (image, label) in enumerate(valid_dloader):
        image = image.float().cuda()
        label = label.long().cuda()
        label_onehot = torch.zeros(label.size(0), discrete_latent_dim).cuda().scatter_(1, label.view(-1, 1), 1)
        batch_size = image.size(0)
        with torch.no_grad():
            reconstruction, norm_mean, norm_log_sigma, disc_log_alpha, *_ = model(image)
        reconstruct_loss, continuous_kl_loss, discrete_kl_loss = elbo_criterion(image, reconstruction, norm_mean,
                                                                                norm_log_sigma, disc_log_alpha)
        cls_loss = cls_criterion(disc_log_alpha, label_onehot)
        if args.br:
            mse_loss = F.mse_loss(torch.sigmoid(reconstruction.detach()), image.detach(),
                                  reduction="sum") / (
                               2 * image.size(0) * (args.x_sigma ** 2))
            mse_losses.update(float(mse_loss), image.size(0))
        all_score.append(torch.exp(disc_log_alpha))
        all_label.append(label_onehot)
        reconstruct_losses.update(float(reconstruct_loss), batch_size)
        continuous_kl_losses.update(float(continuous_kl_loss.item()), batch_size)
        discrete_kl_losses.update(float(discrete_kl_loss.item()), batch_size)
        cls_losses.update(float(cls_loss.item()), batch_size)

    writer.add_scalar(tag="Valid/cls_loss", scalar_value=cls_losses.avg, global_step=epoch + 1)
    writer.add_scalar(tag="Valid/reconstruct_loss", scalar_value=reconstruct_losses.avg, global_step=epoch + 1)
    writer.add_scalar(tag="Valid/continuous_kl_loss", scalar_value=continuous_kl_losses.avg, global_step=epoch + 1)
    writer.add_scalar(tag="Valid/discrete_kl_loss", scalar_value=discrete_kl_losses.avg, global_step=epoch + 1)
    if args.br:
        writer.add_scalar(tag="Valid/mse", scalar_value=mse_losses.avg,
                          global_step=epoch)
    all_score = torch.cat(all_score, dim=0).detach()
    all_label = torch.cat(all_label, dim=0).detach()
    _, y_true = torch.topk(all_label, k=1, dim=1)
    _, y_pred = torch.topk(all_score, k=5, dim=1)
    # calculate accuracy by hand
    valid_top_1_accuracy = float(torch.sum(y_true == y_pred[:, :1]).item()) / y_true.size(0)
    valid_top_5_accuracy = float(torch.sum(y_true == y_pred).item()) / y_true.size(0)
    writer.add_scalar(tag="Valid/top1 accuracy", scalar_value=valid_top_1_accuracy, global_step=epoch + 1)
    if args.dataset == "Cifar100":
        writer.add_scalar(tag="Valid/top 5 accuracy", scalar_value=valid_top_5_accuracy, global_step=epoch + 1)
    if epoch % args.reconstruct_freq == 0:
        with torch.no_grad():
            image = utils.make_grid(image[:4, ...], nrow=2)
            reconstruct_image = utils.make_grid(torch.sigmoid(reconstruction[:4, ...]), nrow=2)
        writer.add_image(tag="Valid/Raw_Image", img_tensor=image, global_step=epoch + 1)
        writer.add_image(tag="Valid/Reconstruct_Image", img_tensor=reconstruct_image, global_step=epoch + 1)

    return valid_top_1_accuracy, valid_top_5_accuracy


def test(test_dloader, model, elbo_criterion, cls_criterion, epoch, writer, discrete_latent_dim):
    reconstruct_losses = AverageMeter()
    continuous_kl_losses = AverageMeter()
    discrete_kl_losses = AverageMeter()
    mse_losses = AverageMeter()
    cls_losses = AverageMeter()
    model.eval()
    all_score = []
    all_label = []

    for i, (image, label) in enumerate(test_dloader):
        image = image.float().cuda()
        label = label.long().cuda()
        label_onehot = torch.zeros(label.size(0), discrete_latent_dim).cuda().scatter_(1, label.view(-1, 1), 1)
        batch_size = image.size(0)
        with torch.no_grad():
            reconstruction, norm_mean, norm_log_sigma, disc_log_alpha, *_ = model(image)
        reconstruct_loss, continuous_kl_loss, discrete_kl_loss = elbo_criterion(image, reconstruction, norm_mean,
                                                                                norm_log_sigma, disc_log_alpha)
        cls_loss = cls_criterion(disc_log_alpha, label_onehot)
        if args.br:
            mse_loss = F.mse_loss(torch.sigmoid(reconstruction.detach()), image.detach(),
                                  reduction="sum") / (
                               2 * image.size(0) * (args.x_sigma ** 2))
            mse_losses.update(float(mse_loss), image.size(0))
        all_score.append(torch.exp(disc_log_alpha))
        all_label.append(label_onehot)
        reconstruct_losses.update(float(reconstruct_loss), batch_size)
        continuous_kl_losses.update(float(continuous_kl_loss.item()), batch_size)
        discrete_kl_losses.update(float(discrete_kl_loss.item()), batch_size)
        cls_losses.update(float(cls_loss.item()), batch_size)

    writer.add_scalar(tag="Test/cls_loss", scalar_value=cls_losses.avg, global_step=epoch + 1)
    writer.add_scalar(tag="Test/reconstruct_loss", scalar_value=reconstruct_losses.avg, global_step=epoch + 1)
    writer.add_scalar(tag="Test/continuous_kl_loss", scalar_value=continuous_kl_losses.avg, global_step=epoch + 1)
    writer.add_scalar(tag="Test/discrete_kl_loss", scalar_value=discrete_kl_losses.avg, global_step=epoch + 1)
    if args.br:
        writer.add_scalar(tag="Test/mse", scalar_value=mse_losses.avg,
                          global_step=epoch)
    all_score = torch.cat(all_score, dim=0).detach()
    all_label = torch.cat(all_label, dim=0).detach()
    _, y_true = torch.topk(all_label, k=1, dim=1)
    _, y_pred = torch.topk(all_score, k=5, dim=1)
    # calculate accuracy by hand
    test_top_1_accuracy = float(torch.sum(y_true == y_pred[:, :1]).item()) / y_true.size(0)
    test_top_5_accuracy = float(torch.sum(y_true == y_pred).item()) / y_true.size(0)
    writer.add_scalar(tag="Test/top1 accuracy", scalar_value=test_top_1_accuracy, global_step=epoch + 1)
    if args.dataset == "Cifar100":
        writer.add_scalar(tag="Test/top 5 accuracy", scalar_value=test_top_5_accuracy, global_step=epoch + 1)
    if epoch % args.reconstruct_freq == 0:
        with torch.no_grad():
            image = utils.make_grid(image[:4, ...], nrow=2)
            reconstruct_image = utils.make_grid(torch.sigmoid(reconstruction[:4, ...]), nrow=2)
        writer.add_image(tag="Test/Raw_Image", img_tensor=image, global_step=epoch + 1)
        writer.add_image(tag="Test/Reconstruct_Image", img_tensor=reconstruct_image, global_step=epoch + 1)

    return test_top_1_accuracy, test_top_5_accuracy


def modify_lr_rate(opt, lr):
    for param_group in opt.param_groups:
        param_group['lr'] = lr


def alpha_schedule(epoch, max_epoch, alpha_max, strategy="exp"):
    if strategy == "linear":
        alpha = alpha_max * min(1, epoch / max_epoch)
    elif strategy == "exp":
        alpha = alpha_max * math.exp(-5 * (1 - min(1, epoch / max_epoch)) ** 2)
    else:
        raise NotImplementedError("Strategy {} not implemented".format(strategy))
    return alpha


if __name__ == "__main__":
    main()
