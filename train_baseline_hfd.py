import argparse
import os
from math import log10

import pandas as pd
import torch.optim as optim
import torch.utils.data
import torchvision.utils as utils
from torch.autograd import Variable
from torch.utils.data import DataLoader
from tqdm import tqdm
from torch import Tensor
import pytorch_ssim
import numpy as np
from data_utils import TrainDatasetFromFolder, ValDatasetFromFolder, display_transform
from model.loss import GeneratorLoss
from model.baseline_model import Generator, Discriminator

import pywt

parser = argparse.ArgumentParser(description='Train Super Resolution Models')
parser.add_argument('--crop_size', default=256, type=int, help='training images crop size')
parser.add_argument('--upscale_factor', default=8, type=int, choices=[2, 4, 8],
                    help='super resolution upscale factor')
parser.add_argument('--num_epochs', default=100, type=int, help='train epoch number')


if __name__ == '__main__':
    opt = parser.parse_args()

    CROP_SIZE = opt.crop_size
    UPSCALE_FACTOR = opt.upscale_factor
    NUM_EPOCHS = opt.num_epochs

    train_set = TrainDatasetFromFolder('data/DIV2K_train_HR', crop_size=CROP_SIZE, upscale_factor=UPSCALE_FACTOR)
    val_set = ValDatasetFromFolder('data/DIV2K_valid_HR', crop_size=CROP_SIZE*2, upscale_factor=UPSCALE_FACTOR)
    train_loader = DataLoader(dataset=train_set, num_workers=4, batch_size=4, shuffle=True)
    val_loader = DataLoader(dataset=val_set, num_workers=4, batch_size=1, shuffle=False)

    netG = Generator(UPSCALE_FACTOR)
    print('# generator parameters:', sum(param.numel() for param in netG.parameters()))
    netD = Discriminator()
    print('# discriminator parameters:', sum(param.numel() for param in netD.parameters()))
    netD_HF = Discriminator()

    generator_criterion = GeneratorLoss()
    refinement_criterion = torch.nn.L1Loss()

    if torch.cuda.is_available():
        netG.cuda()
        netD.cuda()
        netD_HF.cuda()
        generator_criterion.cuda()
        refinement_criterion.cuda()

    optimizerG = optim.Adam(netG.parameters())
    optimizerD = optim.Adam(netD.parameters())
    optimizerD_HF = optim.Adam(netD_HF.parameters())

    results = {'d_loss': [], 'g_loss': [], 'd_score': [], 'g_score': [], 'psnr': [], 'ssim': []}

    for epoch in range(1, NUM_EPOCHS + 1):
        train_bar = tqdm(train_loader)
        running_results = \
            {'batch_sizes': 0, 'd_loss': 0, 'g_loss': 0, 'd_score': 0, 'g_score': 0, 'd_loss_hf': 0}


        netG.train()
        netD.train()
        netD_HF.train()
        it = 0
        for data, target in train_bar:
            it += 1
            g_update_first = True
            batch_size = data.size(0)
            running_results['batch_sizes'] += batch_size

            ############################
            # (1) Update D network: maximize D(x)-1-D(G(z))
            ###########################
            real_img = Variable(target)
            if torch.cuda.is_available():
                real_img = real_img.cuda()
            z = Variable(data)
            if torch.cuda.is_available():
                z = z.cuda()
            fake_img = netG(z)

            netD.zero_grad()
            real_out = torch.sigmoid(netD(real_img)).mean()
            fake_out = torch.sigmoid(netD(fake_img)).mean()
            d_loss = 1 - real_out + fake_out
            d_loss.backward(retain_graph=True)
            optimizerD.step()

            ############################
            # (2) Update D HF network: maximize D(x)-1-D(G(z))
            ###########################
            hf_transform = pywt.dwt2(target, 'bior1.3')[1]
            real_img_hf_1 = Variable(torch.from_numpy(hf_transform[0]))
            real_img_hf_2 = Variable(torch.from_numpy(hf_transform[1]))
            real_img_hf_3 = Variable(torch.from_numpy(hf_transform[2]))

            if torch.cuda.is_available():
                real_img_hf_1 = real_img_hf_1.cuda()
                real_img_hf_2 = real_img_hf_2.cuda()
                real_img_hf_3 = real_img_hf_3.cuda()
            z = Variable(data).cuda()
            fake_img = netG(z)
            hf_transform_fake_img = pywt.dwt2(fake_img.cpu().detach().numpy() , 'bior1.3')[1]
            fake_img_1 = Variable(torch.from_numpy(hf_transform_fake_img[0]))
            fake_img_2 = Variable(torch.from_numpy(hf_transform_fake_img[1]))
            fake_img_3 = Variable(torch.from_numpy(hf_transform_fake_img[2]))
            if torch.cuda.is_available():
                fake_img_1 = fake_img_1.cuda()
                fake_img_2 = fake_img_2.cuda()
                fake_img_3 = fake_img_3.cuda()

            valid_hf = Variable(Tensor(np.ones((12, 1, 9, 9))), requires_grad=False).cuda()
            fake_hf = Variable(Tensor(np.zeros((12, 1, 9, 9))), requires_grad=False).cuda()

            netD_HF.zero_grad()
            real_out_hf = torch.sigmoid(netD_HF(torch.cat([real_img_hf_1, real_img_hf_2, real_img_hf_3], dim=0)).cuda())
            fake_out_hf = torch.sigmoid(netD_HF(torch.cat([fake_img_1, fake_img_2, fake_img_3], dim=0)).cuda())
            d_loss_hf = (1 - real_out_hf + fake_out_hf).mean()
            d_loss_hf.backward(retain_graph=True)
            optimizerD_HF.step()


            ############################
            # (2) Update G network: minimize 1-D(G(z)) + Perception Loss + Image Loss + TV Loss
            ###########################
            netG.zero_grad()
            g_loss = generator_criterion(fake_out, fake_img, real_img, fake_out_hf=fake_out_hf)
            g_loss.backward()


            optimizerG.step()


            # loss for current batch before optimization
            running_results['g_loss'] += g_loss.item() * batch_size
            running_results['d_loss'] += d_loss.item() * batch_size
            running_results['d_score'] += real_out.item() * batch_size
            running_results['g_score'] += fake_out.item() * batch_size
            running_results['d_loss_hf'] += d_loss_hf.item() * batch_size

            train_bar.set_description(desc=
                                      '[%d/%d] Loss_D: %.4f Loss_G: %.4f Loss_D_HF: %.4f'
                                      'D(x): %.4f D(G(z)): %.4f '
                                      % (
                epoch, NUM_EPOCHS,
                running_results['d_loss'] / running_results['batch_sizes'],
                running_results['g_loss'] / running_results['batch_sizes'],
                running_results['d_loss_hf'] / running_results['batch_sizes'],
                running_results['d_score'] / running_results['batch_sizes'],
                running_results['g_score'] / running_results['batch_sizes']
                                      ))


            batches_done = epoch * 200 + it

            if batches_done%100 == 0:
                imgs_lr = torch.nn.functional.interpolate(z, scale_factor=8)
                gen_hr = utils.make_grid(fake_img, nrow=1, normalize=True)
                imgs_lr = utils.make_grid(imgs_lr, nrow=1, normalize=True)
                real_hr = utils.make_grid(real_img, nrow=1, normalize=True)
                img_grid = torch.cat((imgs_lr, gen_hr, real_hr), -1)
                utils.save_image(img_grid, "images/%d.png" % batches_done, normalize=False)

        netG.eval()
        out_path = 'training_results/SRF_' + str(UPSCALE_FACTOR) + '/'
        if not os.path.exists(out_path):
            os.makedirs(out_path)

        with torch.no_grad():
            val_bar = tqdm(val_loader)
            valing_results = \
                {'mse': 0,
                 'ssims': 0,
                 'psnr': 0,
                 'ssim': 0,
                 'batch_sizes': 0}
            val_images = []
            for val_lr, val_hr_restore, val_hr in val_bar:
                batch_size = val_lr.size(0)
                valing_results['batch_sizes'] += batch_size
                lr = val_lr
                hr = val_hr
                if torch.cuda.is_available():
                    lr = lr.cuda()
                    hr = hr.cuda()
                sr = netG(lr)

                batch_mse = ((sr - hr) ** 2).data.mean()
                valing_results['mse'] += batch_mse * batch_size
                batch_ssim = pytorch_ssim.ssim(sr, hr).item()
                valing_results['ssims'] += batch_ssim * batch_size
                valing_results['psnr'] = \
                    10 * log10((hr.max()**2) / (valing_results['mse'] / valing_results['batch_sizes']))
                valing_results['ssim'] = valing_results['ssims'] / valing_results['batch_sizes']
                val_bar.set_description(
                    desc='[converting LR images to SR images] PSNR: %.4f dB SSIM: %.4f' % (
                        valing_results['psnr'], valing_results['ssim']))

                val_images.extend(
                    [display_transform()(val_hr_restore.squeeze(0)), display_transform()(hr.data.cpu().squeeze(0)),
                     display_transform()(sr.data.cpu().squeeze(0))])
            val_images = torch.stack(val_images)
            val_images = torch.chunk(val_images, val_images.size(0) // 15)
            val_save_bar = tqdm(val_images, desc='[saving training results]')
            index = 1
            for image in val_save_bar:
                image = utils.make_grid(image, nrow=3, padding=5)
                utils.save_image(image, out_path + 'epoch_%d_index_%d.png' % (epoch, index), padding=5)
                index += 1

        # save model parameters
        #torch.save(netG.state_dict(), 'epochs/netG_epoch_%d_%d.pth' % (UPSCALE_FACTOR, epoch))
        #torch.save(netD.state_dict(), 'epochs/netD_epoch_%d_%d.pth' % (UPSCALE_FACTOR, epoch))
        # save loss\scores\psnr\ssim
        results['d_loss'].append(running_results['d_loss'] / running_results['batch_sizes'])
        results['g_loss'].append(running_results['g_loss'] / running_results['batch_sizes'])
        results['d_score'].append(running_results['d_score'] / running_results['batch_sizes'])
        results['g_score'].append(running_results['g_score'] / running_results['batch_sizes'])
        results['psnr'].append(valing_results['psnr'])
        results['ssim'].append(valing_results['ssim'])

        if epoch % 10 == 0 and epoch != 0:
            out_path = 'statistics/'
            data_frame = pd.DataFrame(
                data={'Loss_D': results['d_loss'], 'Loss_G': results['g_loss'], 'Score_D': results['d_score'],
                      'Score_G': results['g_score'], 'PSNR': results['psnr'], 'SSIM': results['ssim']},
                index=range(1, epoch + 1))
            data_frame.to_csv(out_path + 'srf_' + str(UPSCALE_FACTOR) + '_train_results.csv', index_label='Epoch')
