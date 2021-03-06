#-*-coding:utf-8 -*-
from collections import defaultdict
import torch
import random
import torch.nn as nn
import pandas as pd
from torch.optim.lr_scheduler import ReduceLROnPlateau
from sklearn.metrics import confusion_matrix, f1_score

from data_preprocess import *
from pytorch_pretrained_bert.modeling import BertForSequenceClassification

device = torch.device('cpu')

def seed_torch(seed=123456789):
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.manual_seed(seed)
    torch.backends.cudnn.deterministic = True

def _set_data_type_to_tensor(data):
    if type(data) == np.ndarray:
        data = torch.from_numpy(data.astype(np.int_)).long().cpu()
    elif type(data) == torch.Tensor:
        source_data_type = torch.LongTensor
    elif type(data) in [list, tuple]:
        # unpack list recursively and convert each element
        data = [_set_data_type_to_tensor(x) for x in data]
    else:
        assert False, 'Unknown data type: not numpy or torch.tensor'
    return data

def _num_records(x_data, y_data, num_records=None):
    """检查输入的x_data和y_data条数相同"""
    if type(x_data) in [list, tuple]:
        for x in x_data:
            num_records = _num_records(x, y_data, num_records)
    else:
        if num_records is None:
            num_records = x_data.size(0)
            if y_data is not None:
                assert num_records == y_data.size(0), "data and labels must be the same size"
                num_records = y_data.size(0)
        else:
            assert num_records == x_data.size(0), "all inputs sets must have same number of records"
            num_records = x_data.size(0)
    return num_records


def r_f1_thresh(y_pred, y_true, step=100):
    # f1值和阈值有关
    e = np.zeros((len(y_true), 2))
    e[:, 0] = y_pred.reshape(-1)
    e[:, 1] = y_true
    f = pd.DataFrame(e)
    thrs = np.linspace(0, 1, step+1)
    x = np.array([f1_score(y_pred=f.loc[:, 0] > thr, y_true=f.loc[:, 1]) for thr in thrs])
    f1_, thresh = max(x), thrs[x.argmax()]
    return f.corr()[0][1], f1_, thresh


def train_fc(model, train_dataset, y, batch_size, criterion, opt):
    model.train()
    train_dataset_list = [train_dataset['input_id'], train_dataset['segment_id'], train_dataset['input_mask']]
    correct, train_loss = 0, 0
    y_pred, y_true = None, None
    # 标签转换为tensor
    y = _set_data_type_to_tensor(y)
    num_records = None
    for i in range(len(train_dataset_list)):
        # 三种类型输入转换为tensor
        train_dataset_list[i] = _set_data_type_to_tensor(train_dataset_list[i])
        # 数据总数量
        num_records = _num_records(train_dataset_list[i], y, num_records)
    num_batches = int((num_records - 1)/ batch_size)        # 计算共有几个批次
    print("num_batches: ", num_batches)
    for batch in range(num_batches):
        # 数据分批
        batch_start = batch * batch_size
        batch_end = (batch+1) * batch_size
        if batch_end > num_records:
            batch_end = num_records
        ixs = slice(batch_start, batch_end)
        x_batch_data = []
        for i in range(len(train_dataset_list)):
            x_batch_data.append(train_dataset_list[i][ixs])
        target = y[ixs]
        # 分批次数据送入模型中
        x_out = model(x_batch_data[0], x_batch_data[1], x_batch_data[2])
        # 优化器梯度归0
        opt.zero_grad()
        # 计算每批损失
        batch_loss = criterion(x_out, target.long())
        # 梯度回传
        batch_loss.backward()
        train_loss += batch_loss.item()
        print("batch_loss: ", batch_loss.item(), "batch:", batch)
        # 梯度裁剪
        torch.nn.utils.clip_grad_norm(model.parameters(), max_norm=2)
        # 优化器更新，模型训练
        opt.step()
        # 模型输出
        x_out = nn.Softmax()(x_out)
        # 计算准确率
        correct += (torch.max(x_out, 1)[1].data == target.data).sum()
        x_out_ = x_out[:, 1].data.cpu().numpy()
        label = target.data.cpu().numpy()
        if y_true is None:
            y_true = label
            y_pred = x_out_
        else:
            y_pred = np.concatenate((y_pred, x_out_), axis=0)
            y_true = np.concatenate((y_true, label), axis=0)
    # 计算f1值
    r, f1, thresh = r_f1_thresh(y_pred=y_pred, y_true=y_true)
    print("train_loss: ", train_loss)
    print("train_acc:", correct * 100/num_records)
    print("train_f1: ", f1)
    return train_loss/num_batches, correct * 100/num_records, f1, thresh


def validate(model, val_data, y, batch_size, criterion):
    model.eval()
    valid_dataset_list = [val_data['input_id'], val_data['segment_id'], val_data['input_mask']]
    y = _set_data_type_to_tensor(y)
    y_true, y_pred = None, None
    for i in range(len(valid_dataset_list)):
        valid_dataset_list[i] = _set_data_type_to_tensor(valid_dataset_list[i])
        num_records = _num_records(valid_dataset_list[i], y)
    num_batches = int((num_records - 1)/ batch_size)
    valid_loss = 0
    corrects = 0
    for batch in range(num_batches):
        batch_start = batch * batch_size
        batch_end = (batch + 1) * batch_size
        if batch_end > num_records:
            batch_end = num_records
        ixs = slice(batch_start, batch_end)
        x_batch_data = []
        target = y[ixs]
        for i in range(len(valid_dataset_list)):
            x_batch_data.append(valid_dataset_list[i][ixs])
        # 模型验证以及测试不需要梯度
        with torch.no_grad():
            x_out = model(x_batch_data[0], x_batch_data[1], x_batch_data[2])
            batch_loss = criterion(x_out, target.long())
        valid_loss += batch_loss.item()
        x_out = nn.Softmax()(x_out)

        corrects += (torch.max(x_out, 1)[1].data == target.data).sum()
        x_out_ = x_out[:, 1].data.cpu().numpy()
        label = target.data.cpu().numpy()
        if y_true is None:
            y_true = label
            y_pred = x_out_
        else:
            y_pred = np.concatenate((y_pred, x_out_), axis=0)
            y_true = np.concatenate((y_true, label), axis=0)
        # print("y_true:", y_true)
    r, f1, thresh = r_f1_thresh(y_pred=y_pred, y_true=y_true)
    print("valid_acc:", corrects * 100 / num_records)
    print("valid_f1: ", f1)
    return valid_loss / num_batches, corrects * 100 / num_records, f1, thresh

def test(new_model, test_data, batch_size, best_thresh):
    """完成测试集的标注"""
    new_model.eval()
    y_pred = None
    test_dataset_list = [test_data['input_id'], test_data['segment_id'], test_data['input_mask']]
    for i in range(len(test_dataset_list)):
        test_dataset_list[i] = _set_data_type_to_tensor(test_dataset_list[i])
        num_records = test_dataset_list[i].size(0)
    num_batches = int((num_records - 1) / batch_size) + 1
    for batch in range(num_batches):
        batch_start = batch * batch_size
        batch_end = (batch + 1) * batch_size
        if batch_end > num_records:
            batch_end = num_records
        ixs = slice(batch_start, batch_end)
        x_batch_data = []
        for i in range(len(test_dataset_list)):
            x_batch_data.append(test_dataset_list[i][ixs])
        # 模型验证以及测试不需要梯度
        with torch.no_grad():
            x_out = new_model(x_batch_data[0], x_batch_data[1], x_batch_data[2])
        x_out = nn.Softmax()(x_out)
        x_out_ = x_out[:, 1].data.cpu().numpy()
        if y_pred is None:
            y_pred = x_out_
        else:
            y_pred = np.concatenate((y_pred, x_out_), axis=0)
    print("y_pred:", y_pred)
    pd_data = pd.DataFrame(y_pred)
    result = pd_data > best_thresh
    result = result.astype('int')
    result.to_csv('test_result.csv', index=False, header=False)



def main():
    # 参数设置
    batch_size = 128
    num_epochs = 20
    learning_rate = 0.0005
    # 变量设置
    dataset_name = 'data'        # 数据集文件名，替换成自己的
    train_name, val_name, test_name, train_set, val_set, test_set, num_classes = 'train', 'valid', 'test', ['train'], ['valid'], ['test'], 2
    max_query_len, max_doc_len, max_url_len = defaultdict(int), defaultdict(int), defaultdict(int)


    ######################Load data #########################################
    data_name = ("data_%s_%s_%s" % (dataset_name, train_name, test_name)).lower()
    train_dataset = gen_data2('D:\大三下\web\期末/%s/'% dataset_name, train_set)
    print("Create training set successfully...")
    sval_dataset = gen_data2('D:\大三下\web\期末/%s/'% dataset_name, val_set)
    print("Create valid set successfuly...")
    test_dataset = gen_data2('D:\大三下\web\期末/%s/'% dataset_name, test_set)
    print("Create test set successfully...")
    ######################Training model #########################################
    # 定义模型
    model = BertForSequenceClassification.from_pretrained("bert-large-uncased", num_labels=2)
    model = model.cpu()
    # 定义优化器
    opt = torch.optim.SGD(filter(lambda p: p.requires_grad, model.parameters()), lr=learning_rate,
                          weight_decay=1e-6, momentum=0.9, nesterov=True)
    lr_reducer = ReduceLROnPlateau(optimizer=opt, verbose=True)
    print("use SGD optimizer")
    # 定义损失函数
    criterion = torch.nn.CrossEntropyLoss(ignore_index=-1)
    criterion.cpu()

    try:
        best_acc, best_f1, best_thresh = None, None, None
        print("-" * 90)
        total_start_time = time.time()
        for epoch in range(num_epochs):
            epoch_start_time = time.time()
            # 训练模型
            train_loss, train_acc, train_f1, train_thresh = train_fc(model, train_dataset, train_dataset['sim'], batch_size, criterion, opt)
            # 验证集验证模型
            val_loss, val_acc, val_f1, val_thresh = validate(model, sval_dataset, sval_dataset['sim'], batch_size, criterion)
            print("|start of epoch{:3d} | time : {:2.2f}s | loss {:5.6f} | train_acc {:2.2f} |train_f1 {} | train_thresh {}".format(epoch, time.time()-epoch_start_time,
                                                                                               train_loss, train_acc, train_f1, train_thresh))

            # 设置学习率衰减机制
            lr_reducer.step(val_loss)
            print("-" * 10)
            print("| end of epoch {:3d}| time: {:2.2f}s | loss: {:.4f} | valid_acc {:2.2f} | valid_f1 {} | valid_thresh {}".format(epoch, time.time()-epoch_start_time,
                                                                                                               val_loss, val_acc, val_f1, val_thresh))
            if not best_f1 or best_f1 < val_f1:
                best_f1 = val_f1
                print("save the best model... best_f1: %s" % best_f1)
                last_model_weight = 'checkpoint_BERT.pt'
                print("last_model_weight:", last_model_weight)
                # 保存最佳模型参数
                torch.save(model.state_dict(), last_model_weight)
                best_thresh = val_thresh
    except KeyboardInterrupt:
        print("-" * 90)
        print("Exiting from training early | cost time: {:5.2f}min".format((time.time() - total_start_time) / 60.0))

    ################Test model#########################################
    print("load best model ... ")
    # 定义新模型
    new_model = BertForSequenceClassification.from_pretrained('./', num_labels=2)
    # 加载最佳模型参数赋给新定义的模型
    new_model.load_state_dict(torch.load(last_model_weight), strict=False)
    new_model = new_model.to(device)
    # 测试集测试性能
    test(new_model, test_dataset, batch_size, best_thresh)
    print("-" * 10)
if __name__ == '__main__':
    seed_torch()
    main()

























