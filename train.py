import os
import time
import torch
import torch.nn as nn
import timeit
import numpy as np
import matplotlib.pyplot as plt
from torch.autograd import Variable
import torch.backends.cudnn as cudnn
from argparse import ArgumentParser
# user
from builders.model_builder import build_model
from builders.dataset_builder import build_dataset_train
from utils.utils import setup_seed, init_weight, netParams
from utils.metric import get_iou
from utils.loss import CrossEntropyLoss2d, ProbOhemCrossEntropy2d
from utils.lr_scheduler import WarmupPolyLR
from utils.convert_state import convert_state_dict

#num_worker

GLOBAL_SEED = 1234


def val(args, val_loader, model):
    """
    args:
      val_loader: loaded for validation dataset
      model: model
    return: mean IoU and IoU class
    """
    # evaluation mode
    model.eval()
    total_batches = len(val_loader)

    data_list = []
    for i, (input, label, size, name) in enumerate(val_loader):
        with torch.no_grad():
            input_var = Variable(input).cuda()
        start_time = time.time()
        output = model(input_var)
        time_taken = time.time() - start_time
        print("[%d/%d]  time: %.2f" % (i + 1, total_batches, time_taken))
        output = output.cpu().data[0].numpy()
        gt = np.asarray(label[0].numpy(), dtype=np.uint8)
        output = output.transpose(1, 2, 0)
        output = np.asarray(np.argmax(output, axis=2), dtype=np.uint8)
        data_list.append([gt.flatten(), output.flatten()])

    meanIoU, per_class_iu = get_iou(data_list, args.classes)
    return meanIoU, per_class_iu


def train(args, train_loader, model, criterion, optimizer, epoch):
    """
    args:
       train_loader: loaded for training dataset
       model: model
       criterion: loss function
       optimizer: optimization algorithm, such as ADAM or SGD
       epoch: epoch number
    return: average loss, per class IoU, and mean IoU
    """
    model.train()
    epoch_loss = []

    total_batches = len(train_loader)
    print("=====> the number of iterations per epoch: ", total_batches)
    st = time.time()
    for iteration, batch in enumerate(train_loader, 0):
        args.per_iter = total_batches
        args.max_iter = args.max_epochs * args.per_iter
        args.cur_iter = epoch * args.per_iter + iteration
        scheduler = WarmupPolyLR(optimizer, T_max=args.max_iter, cur_iter=args.cur_iter, warmup_factor=1.0 / 3,
                                 warmup_iters=500, power=0.9)
        lr = optimizer.param_groups[0]['lr']

        start_time = time.time()
        images, labels, _, _ = batch
        images = Variable(images).cuda()
        labels = Variable(labels.long()).cuda()
        output = model(images)
        loss = criterion(output, labels)

        optimizer.zero_grad()  # set the grad to zero
        loss.backward()
        optimizer.step()

        scheduler.step()

        epoch_loss.append(loss.item())
        time_taken = time.time() - start_time

        print('=====> epoch[%d/%d] iter: (%d/%d) \tcur_lr: %.6f loss: %.3f time:%.2f' % (epoch + 1, args.max_epochs,
                                                                                         iteration + 1, total_batches,
                                                                                         lr, loss.item(), time_taken))

    time_taken_epoch = time.time() - st
    remain_time = time_taken_epoch * (args.max_epochs - 1 - epoch)
    m, s = divmod(remain_time, 60)
    h, m = divmod(m, 60)
    print("Remaining training time = %d hour %d minutes %d seconds" % (h, m, s))

    average_epoch_loss_train = sum(epoch_loss) / len(epoch_loss)

    return average_epoch_loss_train, lr


def train_model(args):
    """
    args:
       args: global arguments
    """
    h, w = map(int, args.input_size.split(','))
    input_size = (h, w)
    print("=====> input size:{}".format(input_size))

    print(args)

    if args.cuda:
        print("=====> use gpu id: '{}'".format(args.gpus))
        os.environ["CUDA_VISIBLE_DEVICES"] = args.gpus
        if not torch.cuda.is_available():
            raise Exception("No GPU found or Wrong gpu id, please run without --cuda")

    # set the seed
    setup_seed(GLOBAL_SEED)
    print("=====> set Global Seed: ", GLOBAL_SEED)

    cudnn.enabled = True
    print("=====> building network")

    # build the model and initialization
    model = build_model(args.model, num_classes=args.classes)
    init_weight(model, nn.init.kaiming_normal_,
                nn.BatchNorm2d, 1e-3, 0.1,
                mode='fan_in')

    print("=====> computing network parameters and FLOPs")
    total_paramters = netParams(model)
    print("the number of parameters: %d ==> %.2f M" % (total_paramters, (total_paramters / 1e6)))

    # load data and data augmentation
    datas, trainLoader, valLoader = build_dataset_train(args.dataset, input_size, args.batch_size, args.train_type,
                                                        args.random_scale, args.random_mirror, args.num_workers)

    print('=====> Dataset statistics')
    print("data['classWeights']: ", datas['classWeights'])
    print('mean and std: ', datas['mean'], datas['std'])

    # define loss function, respectively
    weight = torch.from_numpy(datas['classWeights'])

    if args.dataset == 'camvid':
        criteria = CrossEntropyLoss2d(weight=weight, ignore_label=ignore_label)
    elif args.dataset == 'cityscapes':
        min_kept = int(args.batch_size // len(args.gpus) * h * w // 16)
        criteria = ProbOhemCrossEntropy2d(use_weight=True, ignore_label=ignore_label,
                                          thresh=0.7, min_kept=min_kept)
    else:
        raise NotImplementedError(
            "This repository now supports two datasets: cityscapes and camvid, %s is not included" % args.dataset)

    if args.cuda:
        criteria = criteria.cuda()
        if torch.cuda.device_count() > 1:
            print("torch.cuda.device_count()=", torch.cuda.device_count())
            args.gpu_nums = torch.cuda.device_count()
            model = nn.DataParallel(model).cuda()  # multi-card data parallel
        else:
            args.gpu_nums = 1
            print("single GPU for training")
            model = model.cuda()  # 1-card data parallel

    args.savedir = (args.savedir + args.dataset + '/' + args.model + 'bs'
                    + str(args.batch_size) + 'gpu' + str(args.gpu_nums) + "_" + str(args.train_type) + '/')

    if not os.path.exists(args.savedir):
        os.makedirs(args.savedir)

    start_epoch = 0

    # continue training
    if args.resume:
        if os.path.isfile(args.resume):
            checkpoint = torch.load(args.resume)
            start_epoch = checkpoint['epoch']
            model.load_state_dict(checkpoint['model'])
            # model.load_state_dict(convert_state_dict(checkpoint['model']))
            print("=====> loaded checkpoint '{}' (epoch {})".format(args.resume, checkpoint['epoch']))
        else:
            print("=====> no checkpoint found at '{}'".format(args.resume))

    model.train()
    cudnn.benchmark = True

    logFileLoc = args.savedir + args.logFile
    if os.path.isfile(logFileLoc):
        logger = open(logFileLoc, 'a')
    else:
        logger = open(logFileLoc, 'w')
        logger.write("Parameters: %s Seed: %s" % (str(total_paramters), GLOBAL_SEED))
        logger.write("\n%s\t\t%s\t%s\t%s" % ('Epoch', 'Loss(Tr)', 'mIOU (val)', 'lr'))
    logger.flush()

    # define optimization criteria
    if args.dataset == 'camvid':
        optimizer = torch.optim.Adam(
            filter(lambda p: p.requires_grad, model.parameters()), args.lr, (0.9, 0.999), eps=1e-08, weight_decay=2e-4)

    elif args.dataset == 'cityscapes':
        optimizer = torch.optim.SGD(
            filter(lambda p: p.requires_grad, model.parameters()), args.lr, momentum=0.9, weight_decay=1e-4)

    lossTr_list = []
    epoches = []
    mIOU_val_list = []

    print('=====> beginning training')
    for epoch in range(start_epoch, args.max_epochs):
        # training
        lossTr, lr = train(args, trainLoader, model, criteria, optimizer, epoch)
        lossTr_list.append(lossTr)

        # validation
        if epoch % 50 == 0 or epoch == (args.max_epochs - 1):
            epoches.append(epoch)
            mIOU_val, per_class_iu = val(args, valLoader, model)
            mIOU_val_list.append(mIOU_val)
            # record train information
            logger.write("\n%d\t\t%.4f\t\t%.4f\t\t%.7f" % (epoch, lossTr, mIOU_val, lr))
            logger.flush()
            print("Epoch : " + str(epoch) + ' Details')
            print("Epoch No.: %d\tTrain Loss = %.4f\t mIOU(val) = %.4f\t lr= %.6f\n" % (epoch,
                                                                                        lossTr,
                                                                                        mIOU_val, lr))
        else:
            # record train information
            logger.write("\n%d\t\t%.4f\t\t\t\t%.7f" % (epoch, lossTr, lr))
            logger.flush()
            print("Epoch : " + str(epoch) + ' Details')
            print("Epoch No.: %d\tTrain Loss = %.4f\t lr= %.6f\n" % (epoch, lossTr, lr))

        # save the model
        model_file_name = args.savedir + '/model_' + str(epoch + 1) + '.pth'
        state = {"epoch": epoch + 1, "model": model.state_dict()}

        if epoch >= args.max_epochs - 10:
            torch.save(state, model_file_name)
        elif not epoch % 20:
            torch.save(state, model_file_name)

        # draw plots for visualization
        if epoch % 50 == 0 or epoch == (args.max_epochs - 1):
            # Plot the figures per 50 epochs
            fig1, ax1 = plt.subplots(figsize=(11, 8))

            ax1.plot(range(start_epoch, epoch + 1), lossTr_list)
            ax1.set_title("Average training loss vs epochs")
            ax1.set_xlabel("Epochs")
            ax1.set_ylabel("Current loss")

            plt.savefig(args.savedir + "loss_vs_epochs.png")

            plt.clf()

            fig2, ax2 = plt.subplots(figsize=(11, 8))

            ax2.plot(epoches, mIOU_val_list, label="Val IoU")
            ax2.set_title("Average IoU vs epochs")
            ax2.set_xlabel("Epochs")
            ax2.set_ylabel("Current IoU")
            plt.legend(loc='lower right')

            plt.savefig(args.savedir + "iou_vs_epochs.png")

            plt.close('all')

    logger.close()


if __name__ == '__main__':
    start = timeit.default_timer()
    parser = ArgumentParser()
    parser.add_argument('--model', default="DABNet", help="model name: Context Guided Network (CGNet)")
    parser.add_argument('--dataset', default="cityscapes", help="dataset: cityscapes or camvid")
    parser.add_argument('--train_type', type=str, default="train",
                        help="ontrain for training on train set, ontrainval for training on train+val set")
    parser.add_argument('--max_epochs', type=int, default=1000,
                        help="the number of epochs: 300 for train set, 350 for train+val set")
    parser.add_argument('--input_size', type=str, default="512,1024", help="input size of model")
    parser.add_argument('--random_mirror', type=bool, default=True, help="input image random mirror")
    parser.add_argument('--random_scale', type=bool, default=True, help="input image resize 0.5 to 2")
    parser.add_argument('--num_workers', type=int, default=2, help=" the number of parallel threads")
    parser.add_argument('--lr', type=float, default=4.5e-2, help="initial learning rate")
    parser.add_argument('--batch_size', type=int, default=8, help="the batch size is set to 16 for 2 GPUs")
    parser.add_argument('--savedir', default="./checkpoint/", help="directory to save the model snapshot")
    parser.add_argument('--resume', type=str, default="",
                        help="use this file to load last checkpoint for continuing training")
    parser.add_argument('--classes', type=int, default=19,
                        help="the number of classes in the dataset. 19 and 11 for cityscapes and camvid, respectively")
    parser.add_argument('--logFile', default="log.txt", help="storing the training and validation logs")
    parser.add_argument('--cuda', type=bool, default=True, help="running on CPU or GPU")
    parser.add_argument('--gpus', type=str, default="0", help="default GPU devices (0,1)")
    args = parser.parse_args()

    if args.dataset == 'cityscapes':
        args.classes = 19
        args.input_size = '512,1024'
        ignore_label = 255
    elif args.dataset == 'camvid':
        args.classes = 11
        args.input_size = '360,480'
        ignore_label = 11
    else:
        raise NotImplementedError(
            "This repository now supports two datasets: cityscapes and camvid, %s is not included" % args.dataset)

    train_model(args)
    end = timeit.default_timer()
    hour = 1.0 * (end - start) / 3600
    minute = (hour - int(hour)) * 60
    print("training time: %d hour %d minutes" % (int(hour), int(minute)))
