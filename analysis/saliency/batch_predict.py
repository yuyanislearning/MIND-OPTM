#!/usr/bin/env python3
from absl import app, flags
from absl import logging
import random
import numpy as np
import os
import sys
import tensorflow as tf
from tensorflow import keras
import tensorflow_addons as tfa
from datetime import datetime
from tqdm import tqdm
from pprint import pprint

import json
from Bio import SeqIO
import matplotlib.pyplot as plt
import seaborn as sns
import re
from sklearn.metrics import f1_score, precision_recall_curve, auc, roc_auc_score, accuracy_score, confusion_matrix, average_precision_score
from os.path import exists


import pdb
sys.path.append('/local2/yuyan/PTM-Motif/PTM-pattern-finder/')

from src.utils import get_class_weights,  limit_gpu_memory_growth, PTMDataGenerator
from src import utils
from src.model import TransFormerFixEmbed,  RNN_model, TransFormer
from src.tokenization import additional_token_to_index, n_tokens, tokenize_seq, parse_seq, aa_to_token_index, index_to_token
from src.transformer import  positional_encoding

# model_name = 'saved_model/LSTMTransformer/LSTMTransformer_514_multin_layer_3OPTM_r15'
# model_name = 'saved_model/CNN/CNN_514_multin_layer_3CNN_36912/'

OPTM = True
if not OPTM:
    model_name = '/local2/yuyan/PTM-Motif/PTM-pattern-finder/saved_model/LSTMTransformer/LSTMTransformer_514_multin_layer_3_15_fold_random_'#
else:
    model_name = '/local2/yuyan/PTM-Motif/PTM-pattern-finder/saved_model/LSTMTransformer/LSTMTransformer_514_multin_layer_3OPTM_r15_'
fold = 15
avg_weight = False
batch_size_thre = 64

# change it here
class temp_flag():
    def __init__(self, seq_len=514, d_model=128, batch_size=64, model='Transformer',\
         neg_sam=False, dat_aug=False, dat_aug_thres=None, ensemble=False, random_ensemble=False, embedding=False, n_fold=None):
        self.eval = True
        self.seq_len = seq_len
        self.graph = False
        self.fill_cont = None
        self.d_model = d_model
        self.batch_size = batch_size
        self.model = model
        self.neg_sam = neg_sam
        self.dat_aug = dat_aug
        self.dat_aug_thres = dat_aug_thres
        self.ensemble = ensemble
        self.random_ensemble = random_ensemble
        self.embedding = embedding
        self.n_fold = n_fold

def predict(model,seq_len,aug, batch_size, unique_labels, binary=False):
    # predict cases
    ptm_type = {i:p for i, p in enumerate(unique_labels)}

    if binary:# TODO add or remove binary
        y_trues = []
        y_preds = []
    else:
        y_trues = {ptm_type[i]:[] for i in ptm_type}#{ptm_type:np.array:(n_sample,1)}
        y_preds = {ptm_type[i]:[] for i in ptm_type}

    for test_X,test_Y,test_sample_weights in aug:
        y_pred = model.predict(test_X, batch_size=batch_size)
        # seq_len = test_X[0].shape[1]
        if not binary:
            y_mask = test_sample_weights.reshape(-1, seq_len, len(unique_labels))
            y_true = test_Y.reshape(-1, seq_len, len(unique_labels))
            y_pred = y_pred.reshape(-1, seq_len, len(unique_labels))
            for i in range(len(unique_labels)):
                y_true_i = y_true[:,:,i]
                y_pred_i = y_pred[:,:,i]
                y_mask_i = y_mask[:,:,i]

                y_true_i = y_true_i[y_mask_i==1]
                y_pred_i = y_pred_i[y_mask_i==1]
                y_trues[ptm_type[i]].append(y_true_i)
                y_preds[ptm_type[i]].append(y_pred_i)
        else:
            y_mask = test_sample_weights
            y_true = test_Y
    y_trues = {ptm:np.concatenate(y_trues[ptm],axis=0) for ptm in y_trues}
    y_preds = {ptm:np.concatenate(y_preds[ptm],axis=0) for ptm in y_preds}
                
    return y_trues, y_preds    

def ensemble_get_weights(PR_AUCs, unique_labels):
    weights = {ptm:None for ptm in unique_labels}
    for ptm in unique_labels:
        weight = np.array([PR_AUCs[str(i)][ptm] for i in range(len(PR_AUCs))])
        weight = weight/np.sum(weight)
        weights[ptm] = weight
    return weights # {ptm_type}


def cut_protein(sequence, seq_len, uid):
    # cut the protein if it is longer than chunk_size
    # only includes labels within middle chunk_size//2
    # during training, if no pos label exists, ignore the chunk
    # during eval, retain all chunks for multilabel; retain all chunks of protein have specific PTM for binary
    chunk_size = seq_len - 2
    assert chunk_size%4 == 0
    quar_chunk_size = chunk_size//4
    half_chunk_size = chunk_size//2
    records = []
    if len(sequence) > chunk_size:
        for i in range((len(sequence)-1)//half_chunk_size):
            # the number of half chunks=(len(sequence)-1)//chunk_size+1,
            # minus one because the first chunks contains two halfchunks
            max_seq_ind = (i+2)*half_chunk_size
            if i==0:
                cover_range = (0,quar_chunk_size*3)
            elif i==((len(sequence)-1)//half_chunk_size-1):
                cover_range = (quar_chunk_size, len(sequence)-i*half_chunk_size)
                max_seq_ind = len(sequence)
            else:
                cover_range = (quar_chunk_size, quar_chunk_size+half_chunk_size)
            seq = sequence[i*half_chunk_size: max_seq_ind]
            # idx = [j for j in range(len((seq))) if (seq[j] in aa and j >= cover_range[0] and j < cover_range[1])]
            records.append({
                'uid': uid,
                'chunk_id': i,
                'seq': seq,
                # 'idx': idx
            })
    else:
        records.append({
            'uid':uid,
            'chunk_id': 0,
            'seq': sequence,
            # 'idx': [j for j in range(len((sequence))) if sequence[j] in aa]
        })
    return records



def main(argv):
    FLAGS = temp_flag()
    limit_gpu_memory_growth()

    if not OPTM:
        label2aa = {'Hydro_K':'K','Hydro_P':'P','Methy_K':'K','Methy_R':'R','N6-ace_K':'K','Palm_C':'C',
        'Phos_ST':'ST','Phos_Y':'Y','Pyro_Q':'Q','SUMO_K':'K','Ubi_K':'K','glyco_N':'N','glyco_ST':'ST'}
    else:
        label2aa = {"Arg-OH_R":'R',"Asn-OH_N":'N',"Asp-OH_D":'D',"Cys4HNE_C":"C","CysSO2H_C":"C","CysSO3H_C":"C",
            "Lys-OH_K":"K","Lys2AAA_K":"K","MetO_M":"M","MetO2_M":"M","Phe-OH_F":"F",
            "ProCH_P":"P","Trp-OH_W":"W","Tyr-OH_Y":"Y","Val-OH_V":"V"}
    labels = list(label2aa.keys())
    # get unique labels
    unique_labels = sorted(set(labels))
    label_to_index = {str(label): i for i, label in enumerate(unique_labels)}
    index_to_label = {i: str(label) for i, label in enumerate(unique_labels)}
    chunk_size = FLAGS.seq_len - 2

    with open(model_name+'PRAU.json') as f:
        AUPR_dat = json.load(f)
    
    weights = ensemble_get_weights(AUPR_dat, unique_labels)
    
    # models = [tf.keras.models.load_model(model_name)]
    models = [] # load models
    for i in range(fold):#
        models.append(tf.keras.models.load_model(model_name+'fold_'+str(i)))

    y_preds = {}
    chunk_size = FLAGS.seq_len - 2
    quar_chunk_size = chunk_size//4
    half_chunk_size = chunk_size//2

    # for ptm in label2aa.keys():
    # with open('/local2/yuyan/PTM-Motif/Data/OPTM/pig_prot_all.fasta', 'r') as fp:#TODO # /local2/yuyan/PTM-Motif/Data/BCAA/mouse_pdh.fasta
        # for rec in tqdm(SeqIO.parse(fp, 'fasta')): # for every fasta contains phos true label
        #     sequence = str(rec.seq)
        #     uid = str(rec.id)
    # with open('/local2/yuyan/PTM-Motif/Data/OPTM/OPTM_filtered.json') as fp:
    #     dat = json.load(fp)
    
    # for i in range(1):#place holder
    # with open('/local2/yuyan/PTM-Motif/Data/OPTM/nonoverlap_uid.txt') as f:    
    #     for line in tqdm(f):
    #             uid = line.strip()
    #         sequence = dat[uid]['seq']

    if OPTM:
        with open('/local2/yuyan/PTM-Motif/Data/OPTM/OPTM_filtered.json') as f:
            dat = json.load(f)
    else:
        with open('/local2/yuyan/PTM-Motif/Data/Musite_data/ptm/all.json') as f:
            dat = json.load(f)
    count = 0
    records = []
    y_preds = {}
    for uid in tqdm(dat):
        sequence = dat[uid]['seq']
        records.extend(cut_protein(sequence, FLAGS.seq_len, uid))
        count+=1
        if count>batch_size_thre:
            count=0
            seqs = [record['seq'] for record in records]
            uids = [record['uid'] for record in records]
            chunk_ids = [record['chunk_id'] for record in records]
        # records = cut_protein(sequence, FLAGS.seq_len)#
        # if line =='A0A087WPF7.fa':
        #     pdb.set_trace()
        
            # seq = record['seq']
            # idx = record['idx']
            # chunk_id = record['chunk_id']

            X = pad_X(tokenize_seqs(seqs), FLAGS.seq_len)
            X = [X, tf.tile(positional_encoding(FLAGS.seq_len, FLAGS.d_model), [1,1,1])]
            
            for j in range(fold):#fold
                y_pred = models[j](X)#*weights[ptm][j]    
                y_pred = y_pred.numpy().reshape(len(uids), FLAGS.seq_len, -1)
                if avg_weight:
                    temp_weight = 1/fold
                else:
                    temp_weight = np.array([weights[p][j] for p in weights])
                y_pred = y_pred*temp_weight#TODO 
                if j==0:
                    y_pred_sum = y_pred
                else:
                    y_pred_sum += y_pred

            for ptm in label2aa.keys():
                for b, uuid in enumerate(uids):
                    labels = dat[uuid]['label']
                    labels = [label['site'] for label in labels if label['ptm_type']==ptm]
                    chunk_id = chunk_ids[b]
                    seq = seqs[b]
                    if chunk_id==0:
                        cover_range = (0,quar_chunk_size*3)
                    elif chunk_id==((len(sequence)-1)//half_chunk_size-1):
                        cover_range = (quar_chunk_size, len(sequence)-i*half_chunk_size)
                    else:
                        cover_range = (quar_chunk_size, quar_chunk_size+half_chunk_size)
                        
                    idx = [j for j in range(len((seq))) if (seq[j] in label2aa[ptm] and j >= cover_range[0] and j < cover_range[1])]

                    # idx = [j for j in range(len((seq))) if seq[j] in label2aa[ptm]]
                    for i in idx:
                        ix = i+chunk_id*(FLAGS.seq_len-2)//2
                        if y_pred_sum[b, i+1,label_to_index[ptm]]>0.8 and ix in labels:
                            y_preds[str(uid)+'_'+str(ix)+'_'+ptm] = str(y_pred_sum[b, i+1,label_to_index[ptm]])
            records = []
    if not OPTM:
        with open('/local2/yuyan/PTM-Motif/Data/saliency/all_pred_0.8.json','w') as fw:
            json.dump(y_preds, fw)
    else:
        with open('/local2/yuyan/PTM-Motif/Data/saliency/all_pred_OPTM_0.8.json','w') as fw:
            json.dump(y_preds, fw)        
    

# def create_baseline(seq_len):
#     # create an all padding sequence
#     return np.array(seq_len * [additional_token_to_index['<PAD>']])

def pad_X( X, seq_len):
    return np.array([seq_tokens + (seq_len - len(seq_tokens)) * [additional_token_to_index['<PAD>']] for seq_tokens in X])

def tokenize_seqs(seqs):
    # Note that tokenize_seq already adds <START> and <END> tokens.
    return [seq_tokens for seq_tokens in map(tokenize_seq, seqs)]


if __name__ == '__main__':
    app.run(main)
