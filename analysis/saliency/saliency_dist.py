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
import pandas as pd


import pdb

sys.path.append('/local2/yuyan/PTM-Motif/PTM-pattern-finder/')
from src.utils import get_class_weights,  limit_gpu_memory_growth, PTMDataGenerator
from src import utils
from src.model import TransFormerFixEmbed,  RNN_model, TransFormer
from src.tokenization import additional_token_to_index, n_tokens, tokenize_seq, parse_seq, aa_to_token_index, index_to_token
from src.transformer import  positional_encoding


OPTM = False
if not OPTM:
    model_name = '/local2/yuyan/PTM-Motif/PTM-pattern-finder/saved_model/LSTMTransformer/LSTMTransformer_514_multin_layer_3_15_fold_random_'#
else:
    model_name = '/local2/yuyan/PTM-Motif/PTM-pattern-finder/saved_model/LSTMTransformer/LSTMTransformer_514_multin_layer_3OPTM_r15_'
fold = 15

def handle_flags():
    flags.DEFINE_string('model_path',
            'saved_model/LSTMTransformer/LSTMTransformer_514_multin_layer_3_fold_0', 'pretrained model path to load ')
    flags.DEFINE_string('data_path', './', 'path to fasta data')#268 Ubi_K
    flags.DEFINE_string('protein', 'P58871', 'protein uid')
    flags.DEFINE_string('res_path','test', 'path to result dir')
    flags.DEFINE_string('adj_path', '', 'path to structure adjency matrix')
    flags.DEFINE_string("label", 'Phos_ST', "what ptm label")

    # Model parameters.
    flags.DEFINE_bool("graph", False, "use only spatially and locally close sequence (default: False)")#TODO
    flags.DEFINE_bool("ensemble", False, "ensemble learning")#TODO

    # Training parameters.
    flags.DEFINE_integer("seq_len", 514, "maximum lenth+2 of the model sequence (default: 512)")
    flags.DEFINE_integer("d_model", 128, "hidden dimension of the model")
    flags.DEFINE_integer("seq_idx", 1131, "hidden dimension of the model")

    FLAGS = flags.FLAGS


def cut_protein(sequence, seq_len, aa):
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
            idx = [j for j in range(len((seq))) if (seq[j] in aa and j >= cover_range[0] and j < cover_range[1])]
            records.append({
                'chunk_id': i,
                'seq': seq,
                'idx': idx
            })
    else:
        records.append({
            'chunk_id': 0,
            'seq': sequence,
            'idx': [j for j in range(len((sequence))) if sequence[j] in aa]
        })
    return records


def interpolate_emb( emb, alphas, seq_idx, baseline_med='blank', baseline=None):
    # interpolate embedding and baseline
    if baseline is None:
        baseline = get_baseline(emb, baseline_med, seq_idx)
        # baseline = np.zeros(emb.shape)
        if baseline is None:
            return None, None
    else:
        baseline = get_baseline(emb, baseline_med, seq_idx, baseline)
        if baseline is None:
            return None, None
    alphas_x = alphas[:, tf.newaxis, tf.newaxis, tf.newaxis]
    baseline = tf.cast(baseline, emb.dtype)
    emb_x = tf.expand_dims( emb, 0)
    baseline_x = tf.expand_dims(baseline, 0)
    emb_x = tf.tile(emb_x, (1,21,1,1))# match the 21 aa selected in baseline
    delta = emb_x - baseline_x #(1, 21, seq_len, dim)
    embs = baseline_x + alphas_x * delta #(alpha, 21, seq_len, dim)
    seq_len, dim = embs.shape[2], embs.shape[3]
    embs = tf.reshape(embs, (len(alphas)*21, seq_len, dim))# reshape to batch first
    # baseline_x = tf.reshape(baseline_x, (len(alphas)*21, seq_len, dim))
    return embs, baseline

def get_baseline(emb, baseline_med, seq_idx, baseline=None):
    if seq_idx-10<0 or seq_idx+11>emb.shape[1]:
        return None
    if baseline_med =='blank':
        tile_emb = tf.tile(emb, (21,1,1)).numpy() # duplicate the batch
        for i in range(21):
            tile_emb[i,seq_idx-10+i,:] = 0 # set as zero for specific aa
        
    elif baseline_med == 'pad':
        tile_emb = tf.tile(emb, (21,1,1)).numpy()
        baseline = tf.tile(baseline, (21,1,1)).numpy()
        for i in range(21):
            tile_emb[i, seq_idx-10+i, :] = baseline[i, seq_idx-10+i, :] # replace with pad baseline
        
    tile_emb = tf.convert_to_tensor(tile_emb)
    return tile_emb


def get_gradients(X, emb_model,  grad_model, top_pred_idx, seq_idx, embedding=None, method=None, emb=None, baseline=None):
    """Computes the gradients of outputs w.r.t input embedding.

    Args:
        embedding: input embedding
        top_pred_idx: Predicted label for the input image
        seq_idx: location of the label

    Returns:
        Gradients of the predictions w.r.t embedding
    """

    if method == 'gradient':
        embedding = emb_model(X)
        with tf.GradientTape() as tape:
            tape.watch(embedding)
            temp_X = X + [embedding]
            out_pred = grad_model(temp_X)
            top_class = out_pred[0,seq_idx, top_pred_idx] 

        grads = tape.gradient(top_class, embedding)        
        return tf.math.sqrt(tf.math.reduce_mean(tf.math.square(grads), axis = -1)).numpy()

    if method == 'integrated_gradient':
        # batching since it's too big
        alpha, seq_len, dim = embedding.shape[0]//21, embedding.shape[1],embedding.shape[2]
        embedding = tf.reshape(embedding, (alpha, 21, seq_len, dim))
        final_grads = []
        for i in range(21):
            with tf.GradientTape() as tape:
                embed = embedding[:,i,:,:] #(alpha,)
                tape.watch(embed)
                temp_X = [ tf.tile(x, tf.constant([alpha]+(len(x.shape)-1)*[1])) for x in X] + [embed] # tile sequence x to match emb
                out_pred = grad_model(temp_X)
                top_class = out_pred[:,seq_idx, top_pred_idx]
                

            grads = tape.gradient(top_class, embed) # (alpha, seq, dim)
            grads = (grads[:-1] + grads[1:]) / tf.constant(2.0) # calculate integration
            integrated_grads = tf.math.reduce_mean(grads, axis = 0) * (emb[i,:,:] - baseline[i,:,:])  # integration
            final_grads.append(tf.reduce_sum(integrated_grads[ seq_idx-10+i, :], axis=-1).numpy()) #norm of the specific aa

        return np.array(final_grads), top_class[-1]

def heatmap(a, highlight_idx, fle):
    fig, ax = plt.subplots(figsize=(10,5), layout='constrained')
    a = np.squeeze(a)
    ax.plot(list(range(-7,8,1)), a)
    ax.scatter(0, a[7], 50, facecolors='none', edgecolors='black', linewidths=1.5)
    # ax = sns.heatmap(a)
    
    # sns.lineplot(list(range(len(a))), a)
    # plt.plot(highlight_idx, a[highlight_idx], markersize=29, fillstyle='none', markeredgewidth=1.5)
    plt.show()
    plt.savefig(fle)
    plt.close()
    return a



# def create_baseline(seq_len):
#     # create an all padding sequence
#     return np.array(seq_len * [additional_token_to_index['<PAD>']])

def pad_X( X, seq_len):
    return np.array(X + (seq_len - len(X)) * [additional_token_to_index['<PAD>']])

def tokenize_seqs(seqs):
    # Note that tokenize_seq already adds <START> and <END> tokens.
    return [seq_tokens for seq_tokens in map(tokenize_seq, seqs)]

def make_gradcam_heatmap(model, pred_index=None):
    # First, we create a model that maps the input image to the activations
    # of the last conv layer as well as the output predictions
    grad_model = tf.keras.models.Model(
        [model.inputs], [model.get_layer(last_conv_layer_name).output, model.output]
    )

    # Then, we compute the gradient of the top predicted class for our input image
    # with respect to the activations of the last conv layer
    with tf.GradientTape() as tape:
        last_conv_layer_output, preds = grad_model(img_array)
        if pred_index is None:
            pred_index = tf.argmax(preds[0])
        class_channel = preds[:, pred_index]

    # This is the gradient of the output neuron (top predicted or chosen)
    # with regard to the output feature map of the last conv layer
    grads = tape.gradient(class_channel, last_conv_layer_output)

    # This is a vector where each entry is the mean intensity of the gradient
    # over a specific feature map channel
    pooled_grads = tf.reduce_mean(grads, axis=(0, 1, 2))

    # We multiply each channel in the feature map array
    # by "how important this channel is" with regard to the top predicted class
    # then sum all the channels to obtain the heatmap class activation
    last_conv_layer_output = last_conv_layer_output[0]
    heatmap = last_conv_layer_output @ pooled_grads[..., tf.newaxis]
    heatmap = tf.squeeze(heatmap)

    # For visualization purpose, we will also normalize the heatmap between 0 & 1
    heatmap = tf.maximum(heatmap, 0) / tf.math.reduce_max(heatmap)
    return heatmap.numpy()

def ensemble_get_weights(PR_AUCs, unique_labels):
    weights = {ptm:None for ptm in unique_labels}
    for ptm in unique_labels:
        weight = np.array([PR_AUCs[str(i)][ptm] for i in range(len(PR_AUCs))])
        weight = weight/np.sum(weight)
        weights[ptm] = weight
    return weights # {ptm_type}

def dist_plot(embs, fig_path, thres=3):
    out = []
    for i in range(embs.shape[0]):
        out.append(np.argpartition(embs[i,:],-1*thres)[(-1*thres):])
    out = np.concatenate(out)
    dat = pd.DataFrame(data = {'X':out})
    sns.histplot(dat, x='X', discrete=True)
    plt.show()
    plt.savefig(fig_path, dpi=300)
    plt.close()


handle_flags()
def main(argv):
    FLAGS = flags.FLAGS
    limit_gpu_memory_growth()

    label2aa = {'Hydro_K':'K','Hydro_P':'P','Methy_K':'K','Methy_R':'R','N6-ace_K':'K','Palm_C':'C',
    'Phos_ST':'ST','Phos_Y':'Y','Pyro_Q':'Q','SUMO_K':'K','Ubi_K':'K','glyco_N':'N','glyco_ST':'ST'}
    # label2aa = {"Arg-OH_R":'R',"Asn-OH_N":'N',"Asp-OH_D":'D',"Cys4HNE_C":"C","CysSO2H_C":"C","CysSO3H_C":"C",
    #     "Lys-OH_K":"K","Lys2AAA_K":"K","MetO_M":"M","MetO2_M":"M","Phe-OH_F":"F",
    #     "ProCH_P":"P","Trp-OH_W":"W","Tyr-OH_Y":"Y","Val-OH_V":"V"}
    labels = list(label2aa.keys())
    # get unique labels
    unique_labels = sorted(set(labels))
    label_to_index = {str(label): i for i, label in enumerate(unique_labels)}
    index_to_label = {i: str(label) for i, label in enumerate(unique_labels)}


    models = [] # load models
    emb_models = []
    grad_models = []
    for i in range(fold):
        model = tf.keras.models.load_model(model_name+'fold_'+str(i))
        emb_model = keras.models.Model(
            [model.inputs], [model.get_layer('embedding').output]
        )
        
        model_cls = TransFormerFixEmbed( FLAGS.d_model,  num_layers=3, num_heads=8, dff=512, rate=0.1,\
            split_head=False, global_heads=None, fill_cont=None,lstm=True)
        grad_model = model_cls.create_model()
        for layer in model.layers:
            if len(layer.get_weights())!=0 and layer.name!='embedding':
                grad_model.get_layer(layer.name).set_weights(layer.get_weights())
        models.append(model)
        emb_models.append(emb_model)
        grad_models.append(grad_model)
    
    chunk_size = FLAGS.seq_len - 2

    if OPTM:
        with open('/local2/yuyan/PTM-Motif/Data/saliency/all_pred_OPTM_0.8.json') as f:
            dat = json.load(f)
    else:
        with open('/local2/yuyan/PTM-Motif/Data/saliency/all_pred_0.8.json') as f:
            dat = json.load(f)
    with open(model_name+'PRAU.json') as f:
        AUPR_dat = json.load(f)
    
    weights = ensemble_get_weights(AUPR_dat, unique_labels)
    all_saliency = []
    for k in tqdm(dat):
        uid = k.split('_')[0]
        seq_idx = int(k.split('_')[1])
        ptm_type = '_'.join(k.split('_')[2:4])
        with open('/local2/yuyan/PTM-Motif/Data/Musite_data/fasta/'+uid+'.fa') as f:
            sequence = str(list(SeqIO.parse(f, 'fasta'))[0].seq)
        records = cut_protein(sequence, FLAGS.seq_len, label2aa[ptm_type])

        preds = {}
        for record in records:
            seq = record['seq']
            chunk_id = record['chunk_id']

            X = pad_X(tokenize_seq(seq), FLAGS.seq_len)
            X = [tf.expand_dims(X, 0), tf.tile(positional_encoding(FLAGS.seq_len, FLAGS.d_model), [1,1,1])]

            if chunk_id==0:
                cover_range = (0,chunk_size//4*3)
            elif chunk_id==((len(sequence)-1)//-1):
                cover_range = (chunk_size//4+chunk_id*chunk_size//2, len(sequence))
            else:
                cover_range = (chunk_size//4+chunk_id*chunk_size//2, chunk_size//4+(chunk_id+1)*chunk_size//2)


            # only get gradient when the seq_idx fall in the range
            if seq_idx >=cover_range[0] and seq_idx < cover_range[1]:
                # get gradient for specific ptm
                seq_idx = seq_idx - chunk_id*chunk_size//2 +1# padding for zero-based
                # emb_grads = get_gradients(X, emb_model, grad_model, label_to_index[FLAGS.label], seq_idx,method='gradient')
                # heatmap(emb_grads, seq_idx, fle=(fig_name+'_gradient.png', fig_name+'_local_gradient.png'))
                emb_grad_ls = []
                probs = []
                for i in range(fold):
                    # get intergrated gradient for specific ptm
                    emb = emb_models[i](X)
                    m_steps = 50
                    alphas = tf.linspace(start=0.0, stop=1.0, num=m_steps+1) # Generate m_steps intervals for integral_approximation() below.
                    # pad_baseline = [tf.expand_dims(['<PAD>']*FLAGS.seq_len, 0), tf.tile(positional_encoding(FLAGS.seq_len, FLAGS.d_model), [1,1,1])]
                    # interpolated_emb, baseline = interpolate_emb(emb, alphas, pad_baseline)
                    interpolated_emb, baseline = interpolate_emb(emb, alphas, seq_idx)
                    if interpolated_emb is None:
                        continue
                    emb_grads, prob = get_gradients(X, emb_models[i], grad_models[i], label_to_index[ptm_type], \
                        seq_idx, interpolated_emb, method='integrated_gradient', emb=tf.tile(emb,(21,1,1)), baseline=baseline)
                    emb_grad_ls.append(emb_grads)
                    probs.append(prob.numpy())
                emb_grad_ls = np.stack(emb_grad_ls, axis=0) #(15, 21)
                emb_grad_ls = np.expand_dims(weights[ptm_type],-1) * emb_grad_ls#
                emb_grad_ls = np.sum(emb_grad_ls, 0) 
                probs = np.array(probs) #(15)
                probs = weights[ptm_type] * probs
                probs = np.sum(probs)
                

                w_size = 10
                if seq_idx-w_size >= 0 and seq_idx+w_size <=512:
                    emb_grads = np.squeeze(emb_grad_ls)
                    left = seq_idx-w_size
                    right = seq_idx+w_size+1
                    # if left <0:
                    #     left = 0
                    # else:
                    #     seq_idx=w_size
                    # if right > len(emb_grads):
                    #     right=len(emb_grads)
                    emb_grads = emb_grads
                    all_saliency.append(emb_grads)

    all_saliency = np.stack(all_saliency,axis=0)
    np.save('/local2/yuyan/PTM-Motif/Data/saliency/saliency_0.8.npy', all_saliency) 

    # heatmap(emb_grads, seq_idx, fle=(fig_name+'_integrated_gradient.png',fig_name+'_local_integrated_gradient.png'))
    # pprint(preds) Q3SYY2 342 glyco_N




if __name__ == '__main__':
    app.run(main)
