import torch
from torch.autograd import Variable
import torch.nn.functional as F
from .utils import *
from .datasets.utils import label_parser, label_encoder, tstamps_to_dur
try:
    import ahoproc_tools
    from ahoproc_tools.io import *
except ImportError:
    ahoproc_tools = None
from scipy.io import wavfile
import numpy as np
import tempfile
import struct
import json
import timeit
import os


def train_engine(model, dloader, opt, log_freq, train_fn, train_criterion,
                 epochs, save_path, model_savename, tr_opts={}, eval_fn=None, 
                 val_dloader=None, eval_stats=None, eval_target=None, 
                 eval_patience=None, cuda=False, va_opts={}, log_writer=None,
                 opt_scheduler=None):
    tr_loss = {}
    va_loss = {}
    min_va_loss = np.inf
    patience=eval_patience
    for epoch in range(epochs):
        best_model = False
        tr_e_loss = train_fn(model, dloader, opt, log_freq, epoch,
                             criterion=train_criterion,
                             cuda=cuda, tr_opts=tr_opts.copy(),
                             log_writer=log_writer)
        for k, v in tr_e_loss.items():
            if k not in tr_loss:
                tr_loss[k] = [v]
            else:
                tr_loss[k].append(v)
        if eval_fn:
            if val_dloader is None:
                raise ValueError('Train engine: please specify '
                                 'a validation data loader!')
            val_scores = eval_fn(model, val_dloader, 
                                 epoch, cuda=cuda,
                                 stats=eval_stats,
                                 va_opts=va_opts.copy(),
                                 log_writer=log_writer)
            if eval_target:
                if eval_patience is None:
                    raise ValueError('Train engine: Need a patience '
                                     'factor to be specified '
                                     'whem eval_target is given')
                for k, v in val_scores.items():
                    if k not in va_loss:
                        va_loss[k] = [v]
                    else:
                        va_loss[k].append(v)
                if opt_scheduler is not None:
                    opt_scheduler.step(val_scores[eval_target])
                # we have a target key to do early stopping upon it
                if val_scores[eval_target] < min_va_loss:
                    print('Val loss improved {:.3f} -> {:.3f}'
                          ''.format(min_va_loss, val_scores[eval_target]))
                    min_va_loss = val_scores[eval_target]
                    best_model = True
                    patience = eval_patience
                else:
                    patience -= 1
                    print('Val loss did not improve. Curr '
                          'patience: {}/{}'.format(patience,
                                                   eval_patience))
                    if patience == 0:
                        print('Out of patience. Ending training.')
                        break
        model.save(save_path, model_savename, epoch,
                   best_val=best_model)
        for k, v in tr_loss.items():
            #print('Saving training loss ', k)
            np.save(os.path.join(save_path, k), v)
        if eval_target:
            for k, v in va_loss.items():
                #print('Saving val score ', k)
                np.save(os.path.join(save_path, k), v)

def synthesize(dur_model, aco_model, spk_id, spk2durstats, spk2acostats,
               save_path, out_fname, codebooks, lab_file, ogmios_fmt=True, 
               cuda=False, force_dur=False, pf=1):
    beg_t = timeit.default_timer()
    if not force_dur:
        dur_model.eval()
    aco_model.eval()
    lab_parser = label_parser(ogmios_fmt=ogmios_fmt)
    lab_enc = label_encoder(codebooks_path=codebooks,
                            lab_data=None,
                            force_gen=False)
    spk_int = spk_id
    with open(lab_file, 'r') as lf:
        lab_lines = [l.rstrip() for l in lf.readlines()]
    tstamps, parsed_lab = lab_parser(lab_lines)
    lab_codes = []
    for l_n, lab in enumerate(parsed_lab, start=1):
        #print('Encoding[{}]={}'.format(l_n, lab))
        code = lab_enc(lab, normalize='znorm', sort_types=False)
        #print('code[{}]:{}'.format(l_n, code))
        lab_codes.append(code)
    lab_codes = np.array(lab_codes, dtype=np.float32)
    print('lab_codes tensor shape: ', lab_codes.shape)
    # prepare input data
    lab_codes = Variable(torch.from_numpy(lab_codes).unsqueeze(0))
    lab_codes = lab_codes.transpose(0, 1)
    if spk_id is not None:
        spk_id = Variable(torch.LongTensor([spk_id] * lab_codes.size(0)))
        spk_id = spk_id.view(lab_codes.size(0), 1, 1)
    if cuda:
        lab_codes = lab_codes.cuda()
        if spk_id is not None:
            spk_id = spk_id.cuda()

    durstats = spk2durstats[spk_int]
    if force_dur:
        # use durs from lab file
        dur = Variable(torch.FloatTensor(tstamps_to_dur(tstamps, True)))
        dur = dur.view(-1, 1, 1)
        if cuda:
            dur = dur.cuda()
        # normalize durs
        ndurs = (dur - durstats['min']) / \
                (durstats['max'] - durstats['min'])
    else:
        # predict durs
        ndurs, _ = dur_model(lab_codes, None, spk_id)
        min_dur = durstats['min']
        max_dur = durstats['max']
        dur = ndurs * min_dur - max_dur + min_dur

    # build acoustic batch
    aco_inputs = []
    # go over time dur by dur
    for t in range(ndurs.size(0)):
        ndur = np.asscalar(ndurs[t, :, :].cpu().data.numpy())
        # go over all windows within this dur
        reldur_c = 0.
        dur_t = np.asscalar(dur[t, :, :].cpu().data.numpy())
        while reldur_c <= dur_t:
            n_reldur = float(reldur_c) / dur_t
            # every 5ms, shift. TODO: change hardcode to allow speed variation
            reldur_c += 0.005
            aco_inputs.append(np.concatenate((lab_codes[t, 0, :].cpu().data.numpy(),
                                             np.array([n_reldur, ndur]))))
    aco_seqlen = len(aco_inputs)
    aco_inputs = Variable(torch.FloatTensor(aco_inputs))
    aco_inputs = aco_inputs.view(aco_seqlen, 1, -1)
    #print('aco_inputs size: ', aco_inputs.size())
    if cuda:
        aco_inputs = aco_inputs.cuda()
    yt, hstate, ostate = aco_model(aco_inputs,
                                   None, None,
                                   spk_id)
    #np.save('synth_aco_inputs.npy', aco_inputs.squeeze(1).cpu().data.numpy())
    #np.save('synth_aco_outputs.npy', yt.squeeze(1).cpu().data.numpy())
    acostats = spk2acostats[spk_int]
    min_aco = acostats['min']
    max_aco = acostats['max']
    yt_npy = yt.cpu().data.squeeze(1).numpy()
    acot = denorm_minmax(yt_npy, min_aco, max_aco)
    acot = apply_pf(acot, pf, n_feats=40)
    mfcc = acot[:, :40].reshape(-1)
    fv = acot[:, -3].reshape(-1)
    lf0 = acot[:, -2].reshape(-1)
    uv = acot[:, -1].reshape(-1)
    uv = np.round(uv)
    fv[np.where(uv == 0)] = 1000.0
    fv[np.where(fv < 1000)] = 1000.0
    lf0[np.where(uv == 0)] = -10000000000.0
    assert len(uv) == len(fv), 'uv len {} != ' \
                               'fv len {}'.format(len(uv),
                                                    len(fv))
    # write the output ahocoder files
    write_aco_file(os.path.join(save_path, 
                                '{}.cc'.format(out_fname), mfcc))
    write_aco_file(os.path.join(save_path, 
                                '{}.lf0'.format(out_fname), lf0))
    write_aco_file(os.path.join(save_path, 
                                '{}.fv'.format(out_fname), fv))
    aco2wav(os.path.join(save_path, out_fname))
    end_t = timeit.default_timer()
    print('[*] Synthesis completed into file: {}.wav .\n'
          'Total elapsed time: {:.4f} s'.format(out_fname,
                                                end_t - beg_t))


def att_synthesize(dur_model, aco_model, spk_id, spk2durstats, spk2acostats,
                   save_path, out_fname, codebooks, lab_file, ogmios_fmt=True, 
                   cuda=False, force_dur=False, pf=1):
    beg_t = timeit.default_timer()
    if not force_dur:
        dur_model.eval()
    aco_model.eval()
    lab_parser = label_parser(ogmios_fmt=ogmios_fmt)
    lab_enc = label_encoder(codebooks_path=codebooks,
                            lab_data=None,
                            force_gen=False)
    spk_int = spk_id
    with open(lab_file, 'r') as lf:
        lab_lines = [l.rstrip() for l in lf.readlines()]
    tstamps, parsed_lab = lab_parser(lab_lines)
    lab_codes = []
    for l_n, lab in enumerate(parsed_lab, start=1):
        #print('Encoding[{}]={}'.format(l_n, lab))
        code = lab_enc(lab, normalize='minmax', sort_types=False)
        #print('code[{}]:{}'.format(l_n, code))
        lab_codes.append(code)
    lab_codes = np.array(lab_codes, dtype=np.float32)
    print('lab_codes tensor shape: ', lab_codes.shape)
    # prepare input data
    lab_codes = Variable(torch.from_numpy(lab_codes).unsqueeze(0))
    lab_codes = lab_codes.transpose(0, 1)
    if spk_id is not None:
        spk_id = Variable(torch.LongTensor([spk_id] * lab_codes.size(0)))
        spk_id = spk_id.view(lab_codes.size(0), 1, 1)
    if cuda:
        lab_codes = lab_codes.cuda()
        if spk_id is not None:
            spk_id = spk_id.cuda()

    durstats = spk2durstats[spk_int]
    if force_dur:
        # use durs from lab file
        dur = Variable(torch.FloatTensor(tstamps_to_dur(tstamps, True)))
        dur = dur.view(-1, 1, 1)
        if cuda:
            dur = dur.cuda()
        # normalize durs
        ndurs = (dur - durstats['min']) / \
                (durstats['max'] - durstats['min'])
    else:
        # predict durs
        ndurs, _ = dur_model(lab_codes, None, spk_id)
        min_dur = durstats['min']
        max_dur = durstats['max']
        dur = ndurs * min_dur - max_dur + min_dur

    # build acoustic batch
    aco_inputs = []
    # go over time dur by dur
    for t in range(ndurs.size(0)):
        ndur = np.asscalar(ndurs[t, :, :].cpu().data.numpy())
        # go over all windows within this dur
        reldur_c = 0.
        dur_t = np.asscalar(dur[t, :, :].cpu().data.numpy())
        while reldur_c < dur_t:
            n_reldur = float(reldur_c) / dur_t
            # every 5ms, shift. TODO: change hardcode to allow speed variation
            reldur_c += 0.005
            aco_inputs.append(np.concatenate((lab_codes[t, 0, :].cpu().data.numpy(),
                                             np.array([n_reldur, ndur]))))
    aco_seqlen = len(aco_inputs)
    aco_inputs = torch.FloatTensor(aco_inputs)
    aco_inputs = aco_inputs.view(aco_seqlen, 1, -1)
    if cuda:
        aco_inputs = aco_inputs.cuda()
    with torch.no_grad():
        yt = aco_model(aco_inputs, speaker_idx=spk_id)
    print('yt size: ', yt.size())
    acostats = spk2acostats[spk_int]
    min_aco = acostats['min']
    max_aco = acostats['max']
    yt_npy = yt.cpu().data.squeeze(1).numpy()
    acot = denorm_minmax(yt_npy, min_aco, max_aco)
    acot = apply_pf(acot, pf, n_feats=40)
    mfcc = acot[:, :40].reshape(-1)
    fv = acot[:, -3].reshape(-1)
    lf0 = acot[:, -2].reshape(-1)
    uv = acot[:, -1].reshape(-1)
    uv = np.round(uv)
    fv[np.where(uv == 0)] = 1000.0
    fv[np.where(fv < 1000)] = 1000.0
    lf0[np.where(uv == 0)] = -10000000000.0
    assert len(uv) == len(fv), 'uv len {} != ' \
                               'fv len {}'.format(len(uv),
                                                    len(fv))
    # write the output ahocoder files
    write_aco_file(os.path.join(save_path, 
                                '{}.cc'.format(out_fname)), mfcc)
    write_aco_file(os.path.join(save_path, 
                                '{}.lf0'.format(out_fname)), lf0)
    write_aco_file(os.path.join(save_path, 
                                '{}.fv'.format(out_fname)), fv)
    aco2wav(os.path.join(save_path, out_fname))
    end_t = timeit.default_timer()
    print('[*] Synthesis completed into file: {}.wav .\n'
          'Total elapsed time: {:.4f} s'.format(out_fname,
                                                end_t - beg_t))
    

def train_aco_epoch(model, dloader, opt, log_freq, epoch_idx,
                    criterion=None, cuda=False, tr_opts={},
                    spk2acostats=None, log_writer=None):
    # When mulout is True (MO), log_freq is per round, not batch
    # note that a round will have N batches
    model.train()
    global_step = epoch_idx * len(dloader)
    # At the moment, acoustic training is always stateful
    spk2acostats = None
    if 'spk2acostats' in tr_opts:
        print('Getting spk2acostats')
        spk2acostats = tr_opts.pop('spk2acostats')
    idx2spk = None
    if 'idx2spk' in tr_opts:
        idx2spk = tr_opts.pop('idx2spk')
    mulout = False
    round_N = 1
    if 'mulout' in tr_opts:
        print('Multi-Output aco training')
        mulout = tr_opts.pop('mulout')
        round_N = len(list(idx2spk.keys()))
        if idx2spk is None:
            raise ValueError('Specify a idx2spk in training opts '
                             'when using MO.')
    assert len(tr_opts) == 0, 'unrecognized params passed in: '\
                              '{}'.format(tr_opts.keys())
    epoch_losses = {}
    num_batches = len(dloader)
    print('num_batches: ', num_batches)
    # keep stateful references by spk idx
    spk2hid_states = {}
    spk2out_states = {}
    if mulout:
        # keep track of the losses per round to make a proper log
        # when MO is running 
        spk_loss_batch = {}

    for b_idx, batch in enumerate(dloader):
        # decompose the batch into the sub-batches
        spk_b, lab_b, aco_b, slen_b, ph_b = batch
        # build batch of curr_ph to filter out results without sil phones
        # size of curr_ph_b [bsize, seqlen]
        curr_ph_b = [[ph[2] for ph in ph_s] for ph_s in ph_b]
        # convert all into variables and transpose (we want time-major)
        spk_b = spk_b.transpose(0,1)
        spk_name = idx2spk[spk_b.data[0,0].item()]
        lab_b = lab_b.transpose(0,1)
        aco_b = aco_b.transpose(0,1)
        # get curr batch size
        curr_bsz = spk_b.size(1)
        if spk_name not in spk2hid_states:
            # initialize hidden states for this (hidden and out) speaker
            #print('Initializing states of spk ', spk_name)
            hid_state = model.init_hidden_state(curr_bsz)
            out_state = model.init_output_state(curr_bsz)
        else:
            #print('Fetching mulout states of spk ', spk_name)
            # select last spks state in the MO dict and repackage
            # to not backprop the gradients infinite in time
            hid_state = spk2hid_states[spk_name]
            out_state = spk2out_states[spk_name]
            hid_state = repackage_hidden(hid_state, curr_bsz)
            out_state = repackage_hidden(out_state, curr_bsz)
        if cuda:
            spk_b = var_to_cuda(spk_b)
            lab_b = var_to_cuda(lab_b)
            aco_b = var_to_cuda(aco_b)
            slen_b = var_to_cuda(slen_b)
            hid_state = var_to_cuda(hid_state)
            out_state = var_to_cuda(out_state)
        #print(list(out_state.keys()))
        #print('lab_b size: ', lab_b.size())
        # forward through model
        y, hid_state, out_state = model(lab_b, hid_state, out_state, speaker_idx=spk_b)
        if isinstance(y, dict):
            # we have a MO model, pick the right spk
            y = y[spk_name]
            #print('Saving states of spk ', spk_name)
            # save its states
            spk2hid_states[spk_name] = hid_state
            spk2out_states[spk_name] = out_state
        y = y.squeeze(-1)
        loss = criterion(y, aco_b)
        if mulout:
            spk_loss_batch[idx2spk[spk_b[0,0].cpu().data[0]]] = loss.data[0]

        if criterion != F.nll_loss:
            # TODO: add the aco eval
            preds = None
            gtruths = None
            seqlens = None
            spks = None
            # make the silence mask
            sil_mask = None
            preds, gtruths, \
            spks, sil_mask = predict_masked_mcd(y, aco_b, slen_b, 
                                                spk_b, curr_ph_b,
                                                preds, gtruths,
                                                spks, sil_mask,
                                                'pau')
            #print('Tr After batch preds shape: ', preds.shape)
            #print('Tr After batch gtruths shape: ', gtruths.shape)
            #print('Tr After batch sil_mask shape: ', sil_mask.shape)
            # denorm with normalization stats
            assert spk2acostats is not None
            preds, gtruths = denorm_aco_preds_gtruth(preds, gtruths,
                                                     spks, spk2acostats)
            #print('preds[:20] = ', preds[:20])
            #print('gtruths[:20] = ', gtruths[:20])
            #print('pred min: {}, max: {}'.format(preds.min(), preds.max()))
            #print('gtruths min: {}, max: {}'.format(gtruths.min(), gtruths.max()))
            nosil_aco_mcd = mcd(preds[:,:40] * sil_mask, gtruths[:,:40] * sil_mask)
        else:
            nosil_aco_mcd = None
        # compute loss
        if criterion == F.nll_loss:
            raise NotImplementedError('No nll_loss possible')
            #y = y.view(-1, y.size(-1))
            #aco_b = aco_b.view(-1)
            #q_classes = True
        #print('y size: ', y.size())
        #print('aco_b: ', aco_b.size())
        if mulout:
            #print('Keeping {} spk loss'
            #      ' {:.4f}'.format(idx2spk[spk_b[0,0].cpu().data[0]],
            #                                          loss.data[0]))
            spk_loss_batch[idx2spk[spk_b[0,0].item()]] = loss.data[0]
                
        #print('batch {:4d}: loss: {:.5f}'.format(b_idx + 1, loss.data[0]))
        opt.zero_grad()
        loss.backward()
        opt.step()
        #print('y size: ', y.size())
        if (b_idx + 1) % (round_N * log_freq) == 0 or \
           (b_idx + 1) >= num_batches:
            log_mesg = 'batch {:4d}/{:4d} (epoch {:3d})'.format(b_idx + 1,
                                                                num_batches,
                                                                epoch_idx)
            if mulout:
                log_mesg += ' MO losses: ('
                for mok, moloss in spk_loss_batch.items():
                    log_mesg += '{}:{:.3f},'.format(mok, moloss)
                    loss_mo_name = 'mo-{}_tr_loss'.format(mok)
                    if loss_mo_name not in epoch_losses:
                        epoch_losses[loss_mo_name] = []
                    epoch_losses[loss_mo_name].append(moloss)
                    write_scalar_log(moloss, loss_mo_name,
                                     global_step, log_writer)
                log_mesg = log_mesg[:-1] + ')'
            else:
                log_mesg += ' loss {:.5f}'.format(loss.item())
                if nosil_aco_mcd is not None:
                    log_mesg += ', MCD {:.5f} dB'.format(nosil_aco_mcd)
                if 'tr_loss' not in epoch_losses:
                    epoch_losses['tr_loss'] = []
                    if nosil_aco_mcd:
                        epoch_losses['tr_mcd'] = []
                epoch_losses['tr_loss'].append(loss.item())
                write_scalar_log(loss.item(), 'tr_loss',
                                 global_step, log_writer)
                if nosil_aco_mcd:
                    epoch_losses['tr_mcd'].append(nosil_aco_mcd)
                    write_scalar_log(nosil_aco_mcd, 'tr_mcd',
                                     global_step, log_writer)
            print(log_mesg)
        global_step += 1
    end_log = '-- Finished epoch {:4d}, mean losses:'.format(epoch_idx)
    if isinstance(epoch_losses, dict):
        for k, val in epoch_losses.items():
            end_log += ' ({} : {:.5f})'.format(k, np.mean(val))
    end_log += ' --'
    print(end_log)
    return epoch_losses

def train_dur_epoch(model, dloader, opt, log_freq, epoch_idx,
                    criterion=None, cuda=False, tr_opts={},
                    spk2durstats=None, log_writer=None):
    # When mulout is True (MO), log_freq is per round, not batch
    # note that a round will have N batches
    model.train()
    global_step = epoch_idx * len(dloader)
    stateful = False
    if 'stateful' in tr_opts:
        stateful = True
        tr_opts.pop('stateful')
    spk2durstats = None
    if 'spk2durstats' in tr_opts:
        #print('Getting spk2durstats')
        spk2durstats = tr_opts.pop('spk2durstats')
    idx2spk = None
    if 'idx2spk' in tr_opts:
        idx2spk = tr_opts.pop('idx2spk')
    mulout = False
    round_N = 1
    if 'mulout' in tr_opts:
        print('Multi-Output dur training')
        mulout = tr_opts.pop('mulout')
        round_N = len(list(idx2spk.keys()))
        if idx2spk is None:
            raise ValueError('Specify a idx2spk in training opts '
                             'when using MO.')
    assert len(tr_opts) == 0, 'unrecognized params passed in: '\
                              '{}'.format(tr_opts.keys())
    epoch_losses = {}
    num_batches = len(dloader)
    if mulout:
        # keep track of the losses per round to make a proper log
        # when MO is running 
        spk_loss_batch = {}
    for b_idx, batch in enumerate(dloader):
        # decompose the batch into the sub-batches
        spk_b, lab_b, dur_b, slen_b, ph_b = batch
        # build batch of curr_ph to filter out results without sil phones
        # size of curr_ph_b [bsize, seqlen]
        curr_ph_b = [[ph[2] for ph in ph_s] for ph_s in ph_b]
        # convert all into variables and transpose (we want time-major)
        spk_b = spk_b.transpose(0,1)
        lab_b = lab_b.transpose(0,1)
        dur_b = dur_b.transpose(0,1)
        # get curr batch size
        curr_bsz = spk_b.size(1)
        if (stateful and b_idx == 0) or not stateful:
            #print('Initializing recurrent states, e: {}, b: '
            #      '{}'.format(epoch_idx, b_idx))
            # init hidden states of dur model
            states = model.init_hidden_state(curr_bsz)
        if stateful and b_idx > 0:
            #print('Copying recurrent states, e: {}, b: '
            #      '{}'.format(epoch_idx, b_idx))
            #print('states: ', states)
            # copy last states
            states = tuple(st.detach() for st in states)
            #states = repackage_hidden(states, curr_bsz)
        if cuda:
            spk_b = var_to_cuda(spk_b)
            lab_b = var_to_cuda(lab_b)
            dur_b = var_to_cuda(dur_b)
            slen_b = var_to_cuda(slen_b)
            states = var_to_cuda(states)
        # forward through model
        y, states = model(lab_b, states, speaker_idx=spk_b)
        if isinstance(y, dict):
            # we have a MO model, pick the right spk
            spk_name = idx2spk[spk_b.cpu().data[0,0]]
            # print('Extracting y prediction for MO spk ', spk_name)
            y = y[spk_name]
        q_classes = False
        #print('y size: ', y.size())
        #print('states[0] size: ', states[0].size())
        y = y.squeeze(-1)
        if criterion != F.nll_loss:
            preds = None
            gtruths = None
            seqlens = None
            spks = None
            # make the silence mask
            sil_mask = None
            preds, gtruths, \
            spks, sil_mask = predict_masked_rmse(y, dur_b, slen_b, 
                                                 spk_b, curr_ph_b,
                                                 preds, gtruths,
                                                 spks, sil_mask,
                                                 'pau',
                                                 q_classes)
            #print('Tr After batch preds shape: ', preds.shape)
            #print('Tr After batch gtruths shape: ', gtruths.shape)
            #print('Tr After batch sil_mask shape: ', sil_mask.shape)
            # denorm with normalization stats
            assert spk2durstats is not None
            preds, gtruths = denorm_dur_preds_gtruth(preds, gtruths,
                                                     spks, spk2durstats,
                                                     q_classes)
            #print('preds[:20] = ', preds[:20])
            #print('gtruths[:20] = ', gtruths[:20])
            #print('pred min: {}, max: {}'.format(preds.min(), preds.max()))
            #print('gtruths min: {}, max: {}'.format(gtruths.min(), gtruths.max()))
            nosil_dur_rmse = rmse(preds * sil_mask, gtruths * sil_mask) * 1e3
        else:
            nosil_dur_rmse = None
        # compute loss
        if criterion == F.nll_loss:
            y = y.view(-1, y.size(-1))
            dur_b = dur_b.view(-1)
            q_classes = True
        loss = criterion(y, dur_b)
        if mulout:
            #print('Keeping {} spk loss'
            #      ' {:.4f}'.format(idx2spk[spk_b[0,0].cpu().data[0]],
            #                                          loss.data[0]))
            spk_loss_batch[idx2spk[spk_b[0,0].cpu().data[0]]] = loss.data[0]
                
        #print('batch {:4d}: loss: {:.5f}'.format(b_idx + 1, loss.data[0]))
        opt.zero_grad()
        loss.backward()
        opt.step()
        #print('y size: ', y.size())
        if (b_idx + 1) % (round_N * log_freq) == 0 or \
           (b_idx + 1) >= num_batches:
            log_mesg = 'batch {:4d}/{:4d} (epoch {:3d})'.format(b_idx + 1,
                                                                num_batches,
                                                                epoch_idx)
            if mulout:
                log_mesg += ' MO losses: ('
                for mok, moloss in spk_loss_batch.items():
                    log_mesg += '{}:{:.3f},'.format(mok, moloss)
                    loss_mo_name = 'mo-{}_tr_loss'.format(mok)
                    if loss_mo_name not in epoch_losses:
                        epoch_losses[loss_mo_name] = []
                    epoch_losses[loss_mo_name].append(moloss)
                log_mesg = log_mesg[:-1] + ')'
            else:
                log_mesg += ' loss {:.5f}'.format(loss.data[0])
                if nosil_dur_rmse is not None:
                    log_mesg += ', rmse {:.5f} ms'.format(nosil_dur_rmse)
                if 'tr_loss' not in epoch_losses:
                    epoch_losses['tr_loss'] = []
                    if nosil_dur_rmse:
                        epoch_losses['tr_rmse'] = []
                epoch_losses['tr_loss'].append(loss.data[0])
                write_scalar_log(loss.data[0], 'tr_loss',
                                 global_step, log_writer)
                write_histogram_log(dur_b, 'train/dur', global_step, 
                                    log_writer)
                if nosil_dur_rmse:
                    epoch_losses['tr_rmse'].append(nosil_dur_rmse)
                    write_scalar_log(nosil_dur_rmse, 'tr_nosil_dur_rmse',
                                     global_step, log_writer)
            print(log_mesg)
        global_step += 1
    end_log = '-- Finished epoch {:4d}, mean losses:'.format(epoch_idx)
    for k, val in epoch_losses.items():
        end_log += ' ({} : {:.5f})'.format(k, np.mean(val))
    end_log += ' --'
    print(end_log)
    return epoch_losses

def eval_aco_epoch(model, dloader, epoch_idx, cuda=False,
                   stats=None, va_opts={}, log_writer=None,
                   reset_batch_state=False):
    model.eval()
    with torch.no_grad():
        sil_id = 'pau'
        if 'sil_id' in va_opts:
            sil_id = va_opts.pop('sil_id')
        idx2spk = None
        if 'idx2spk' in va_opts:
            idx2spk = va_opts.pop('idx2spk')
        mulout = False
        if 'mulout' in va_opts:
            print('Multi-Output aco evaluation')
            mulout = va_opts.pop('mulout')
            if idx2spk is None:
                raise ValueError('Specify a idx2spk in eval opts '
                                 'when using MO.')
        assert len(va_opts) == 0, 'unrecognized params passed in: '\
                                  '{}'.format(va_opts.keys())
        spk2acostats=stats
        preds = None
        gtruths = None
        seqlens = None
        spks = None
        # make the silence mask
        sil_mask = None
        all_phones = []
        # keep stateful references by spk idx
        spk2hid_states = {}
        spk2out_states = {}
        for b_idx, batch in enumerate(dloader):
            # decompose the batch into the sub-batches
            spk_b, lab_b, aco_b, slen_b, ph_b = batch
            #print('aco_b size: ', aco_b.size())
            #print('aco_b size: ', aco_b.size())
            #print('ph_b: ', ph_b)
            # build batch of curr_ph to filter out results without sil phones
            # size of curr_ph_b [bsize, seqlen]
            curr_ph_b = []
            for ph_s in ph_b:
                phone_seq = []
                for ph in ph_s:
                    phone_seq.append(ph[2])
                    all_phones.append(ph[2])
                curr_ph_b.append(phone_seq)
                #curr_ph_b = [[ph[2] for ph in ph_s] for ph_s in ph_b]
            #print('len(curr_ph_b): ', len(curr_ph_b))
            #print('len(curr_ph_b[0]): ', len(curr_ph_b[0]))
            # convert all into variables and transpose (we want time-major)
            # TODO: write temporally lab_b adn aco_b to compare to synth
            # batches for aco objective eval mismatch
            aco_b_npy = aco_b.data.numpy()
            lab_b_npy = lab_b.data.numpy()
            #np.save('eval_aco_{}.npy'.format(b_idx),
            #        aco_b_npy)
            #np.save('eval_lab_{}.npy'.format(b_idx),
            #        lab_b_npy)
            spk_b = spk_b.transpose(0,1)
            spk_name = idx2spk[spk_b.cpu().data[0,0].item()]
            lab_b = lab_b.transpose(0,1)
            aco_b = aco_b.transpose(0,1)
            # get curr batch size
            curr_bsz = spk_b.size(1)
            # TODO: atm it is NOT stateful
            if spk_name not in spk2hid_states:
                hid_state = model.init_hidden_state(curr_bsz)
                out_state = model.init_output_state(curr_bsz)
                spk2hid_states[spk_name] = hid_state
                spk2out_states[spk_name] = out_state
                #print('Initializing states of spk ', spk_name)
            else:
                #print('Fetching mulout states of spk ', spk_name)
                # select last spks state in the MO dict
                hid_state = spk2hid_states[spk_name]
                out_state = spk2out_states[spk_name]
                hid_state = repackage_hidden(hid_state, curr_bsz)
                out_state = repackage_hidden(out_state, curr_bsz)
            if cuda:
                spk_b = var_to_cuda(spk_b)
                lab_b = var_to_cuda(lab_b)
                aco_b = var_to_cuda(aco_b)
                slen_b = var_to_cuda(slen_b)
                hid_state = var_to_cuda(hid_state)
                out_state = var_to_cuda(out_state)
            # forward through model
            y, hid_state, out_state = model(lab_b, hid_state, 
                                            out_state, 
                                            speaker_idx=spk_b)
            spk_npy = spk_b.cpu().data.numpy()
            #print(spk_npy)
            all_comp = np.all(spk_npy == spk_npy[0, 0]), spk_npy
            assert all_comp
            if isinstance(y, dict):
                # we have a MO model, pick the right spk
                # print('Extracting y prediction for MO spk ', spk_name)
                y = y[spk_name]
                # save its states
                spk2hid_states[spk_name] = hid_state
                spk2out_states[spk_name] = out_state
            if reset_batch_state:
                # reset RNN states after predicting a batch
                del spk2hid_states[spk_name]
                del spk2out_states[spk_name]
            #print('y size: ', y.size())
            #print('aco_b size: ', aco_b.size())
            #print('len(curr_ph_b)= ', len(curr_ph_b))
            preds, gtruths, \
            spks, sil_mask = predict_masked_mcd(y, aco_b, slen_b, 
                                                spk_b, curr_ph_b,
                                                preds, gtruths,
                                                spks, sil_mask,
                                                sil_id)
        print('After batch preds shape: ', preds.shape)
        print('After batch gtruths shape: ', gtruths.shape)
        print('After batch sil_mask shape: ', sil_mask.shape)
        print('After batch spks shape: ', spks.shape)
        print('Sil mask mean: ', sil_mask.mean())
        # denorm with normalization stats
        assert spk2acostats is not None
        preds, gtruths = denorm_aco_preds_gtruth(preds, gtruths,
                                                 spks, spk2acostats)
        aco_mcd = mcd(preds[:,:40], gtruths[:,:40], spks, idx2spk)
        print('U/V preds min: ', preds[:, -1].min())
        print('U/V preds max: ', preds[:, -1].max())
        print('U/V preds mean: ', preds[:, -1].mean())
        print('U/V gtruth min: ', gtruths[:, -1].min())
        print('U/V gtruth max: ', gtruths[:, -1].max())
        print('U/V gtruth mean: ', gtruths[:, -1].mean())
        #print('preds shape: ', preds.shape)
        #print('gtruths shape: ', gtruths.shape)
        aco_afpr = afpr(np.round(preds[:,-1]).reshape(-1, 1),
                        gtruths[:,-1].reshape(-1, 1), spks, 
                        idx2spk)
        aco_f0_rmse, aco_f0_spk = rmse(np.exp(preds[:, -2]), 
                                       np.exp(gtruths[:, -2]),
                                       spks, idx2spk)
        #print('Evaluated aco F0 mRMSE [Hz]: {:.2f}'.format(aco_f0_rmse))
        masked_f0_preds = np.exp(preds[:, -2]).reshape(-1, 1) * sil_mask
        masked_f0_gtruths = np.exp(gtruths[:, -2]).reshape(-1, 1) * sil_mask
        write_histogram_log(np.exp(preds[:, -2]),
                            'F0 predictions',
                            epoch_idx, log_writer)
        write_histogram_log(np.exp(gtruths[:, -2]),
                            'F0 groundtruth',
                            epoch_idx, log_writer)
        nosil_aco_f0_rmse, \
        nosil_aco_f0_spk = rmse(masked_f0_preds,
                                masked_f0_gtruths,
                                spks, idx2spk)
        write_histogram_log(preds[:, :40],
                            'MFCC predictions',
                            epoch_idx, log_writer)
        write_histogram_log(gtruths[:, :40],
                            'MFCC groundtruth',
                            epoch_idx, log_writer)
        masked_cc_preds = preds[:, :40] * sil_mask
        masked_cc_gtruths = gtruths[:, :40] * sil_mask
        nosil_aco_mcd = mcd(masked_cc_preds, masked_cc_gtruths,
                            spks, idx2spk)
        write_histogram_log(preds[:, -1],
                            'U/V predictions',
                            epoch_idx, log_writer)
        write_histogram_log(gtruths[:, -1],
                            'U/V groundtruth',
                            epoch_idx, log_writer)
        masked_uv_preds = np.round(preds[:, -1]).reshape(-1, 1) * sil_mask
        masked_uv_gtruths = gtruths[:, -1].reshape(-1, 1) * sil_mask
        write_histogram_log(preds[:, -3],
                            'FV predictions',
                            epoch_idx, log_writer)
        write_histogram_log(gtruths[:, -3],
                            'FV groundtruth',
                            epoch_idx, log_writer)
        #print('masked_uv_preds shape: ', masked_uv_preds.shape)
        #print('masked_uv_gtruths shape: ', masked_uv_gtruths.shape)
        nosil_aco_afpr = afpr(masked_uv_preds, masked_uv_gtruths,
                              spks, idx2spk)

        #print('Evaluated aco MCD [dB]: {:.3f}'.format(aco_mcd['total']))
        print('========= F0 RMSE =========')
        print('Evaluated aco W/O sil phones ({}) F0 mRMSE [Hz]:'
              '{:.2f}'.format(sil_id, nosil_aco_f0_rmse))
        write_scalar_log(nosil_aco_f0_rmse, 
                         'total_no-silence_F0_rmse_Hz',
                         epoch_idx, log_writer)
        print('Evaluated aco F0 mRMSE of spks: '
              '{}'.format(json.dumps(nosil_aco_f0_spk,
                                     indent=2)))
        if len(nosil_aco_f0_spk) > 1:
            for k, v in nosil_aco_f0_spk.items():
                write_scalar_log(v, '{}_no-silence_F0_rmse_Hz'.format(k),
                                 epoch_idx, log_writer)
        print('========= MCD =========')
        print('Evaluated aco W/O sil phones ({}) MCD [dB]:'
              '{:.3f}'.format(sil_id, nosil_aco_mcd['total']))
        write_scalar_log(nosil_aco_mcd['total'],
                         'total_MCD_dB',
                         epoch_idx, log_writer)
        #print('Evaluated w/ sil MCD of spks: {}'.format(json.dumps(aco_mcd,
        #                                                           indent=2)))
        print('Evaluated W/O sil MCD of spks: {}'.format(json.dumps(nosil_aco_mcd,
                                                                    indent=2)))
        if len(nosil_aco_mcd) > 2:
            # will print all speakers
            for k, v in nosil_aco_mcd.items():
                if k == 'total':
                    continue
                write_scalar_log(v,
                                 'MCD_spk{}_dB'.format(k),
                                 epoch_idx, log_writer)
        print('========= Acc =========')
        #print('Evaluated aco AFPR [norm]: '.format(aco_afpr['A.total']))
        print('Evaluated W/O sil phones ({}) Acc [norm]:'
              '{}'.format(sil_id, nosil_aco_afpr['A.total']))
        write_scalar_log(nosil_aco_afpr['A.total'], 'Total Accuracy',
                         epoch_idx, log_writer)
        print('Evaluated W/O sil phones ({}) P [norm]:'
              '{}'.format(sil_id, nosil_aco_afpr['P.total']))
        write_scalar_log(nosil_aco_afpr['P.total'], 'Total Precision',
                         epoch_idx, log_writer)
        print('Evaluated W/O sil phones ({}) R [norm]:'
              '{}'.format(sil_id, nosil_aco_afpr['R.total']))
        write_scalar_log(nosil_aco_afpr['R.total'], 'Total Recall',
                         epoch_idx, log_writer)
        print('Evaluated W/O sil phones ({}) F1 [norm]:'
              '{}'.format(sil_id, nosil_aco_afpr['F.total']))
        write_scalar_log(nosil_aco_afpr['F.total'], 'total F1',
                         epoch_idx, log_writer)
        print('=' * 30)
        # WRITE AUDIO TO TBOARD if possible
        if log_writer is not None:
            tfl = tempfile.NamedTemporaryFile()
            cc = preds[:, :40]
            fv = preds[:, -3]
            lf0 = preds[:, -2]
            write_aco_file('{}.cc'.format(tfl.name), cc)
            write_aco_file('{}.fv'.format(tfl.name), fv)
            write_aco_file('{}.lf0'.format(tfl.name), lf0)
            aco2wav('{}'.format(tfl.name))
            rate, wav = wavfile.read('{}.wav'.format(tfl.name))
            # norm in wav
            wav = np.array(wav, dtype=np.float32) / 32767.
            # trim to max of 10 seconds
            wav = wav[:min(wav.shape[0], int(rate * 10))]
            log_writer.add_audio('eval_synth_audio',
                                 wav,
                                 epoch_idx,
                                 sample_rate=rate)
            # remove tmp files
            os.unlink('{}.cc'.format(tfl.name))
            os.unlink('{}.fv'.format(tfl.name))
            os.unlink('{}.lf0'.format(tfl.name))
            os.unlink('{}.wav'.format(tfl.name))
        #print('Evaluated w/ sil MCD of spks: {}'.format(json.dumps(aco_mcd,
        #                                                           indent=2)))
        #print('Evaluated w/o sil MCD of spks: {}'.format(json.dumps(nosil_aco_mcd,
        #                                                            indent=2)))
        #print('Evaluated w/ sil AFPR of spks: {}'.format(json.dumps(aco_afpr,
        #                                                            indent=2)))
        #print('Evaluated w/o sil AFPR of spks: '
        #      '{}'.format(json.dumps(nosil_aco_afpr,
        #                             indent=2)))
        # transform nosil_aco_mcd keys
        new_keys_d = {}
        for k in nosil_aco_mcd.keys():
            if k == 'total':
                # skip this key
                continue
            if mulout:
                # transform each key into the desired loss filename 
                new_keys_d['mo-{}_va_mcd'.format(k)] = nosil_aco_mcd[k]
            else:
                # transform each key into the desired loss filename 
                new_keys_d['so-{}_va_mcd'.format(k)] = nosil_aco_mcd[k]
        for k in nosil_aco_afpr.keys():
            if k == 'total':
                continue
            if mulout:
                new_keys_d['mo-{}_va_afpr'.format(k)] = nosil_aco_afpr[k]
            else:
                new_keys_d['so-{}_va_afpr'.format(k)] = nosil_aco_afpr[k]
        for k in nosil_aco_f0_spk.keys():
            if mulout:
                new_keys_d['mo-{}_va_f0rmse'.format(k)] = nosil_aco_f0_spk[k]
            else:
                new_keys_d['so-{}_va_f0rmse'.format(k)] = nosil_aco_f0_spk[k]
        new_keys_d.update({'total_aco_mcd':aco_mcd['total'],
                           'total_nosil_aco_mcd':nosil_aco_mcd['total'],
                           'total_aco_afpr':aco_afpr['total'],
                           'total_nosil_aco_afpr':nosil_aco_afpr['total'],
                           'total_aco_f0rmse':aco_f0_rmse,
                           'total_nosil_aco_f0rmse':nosil_aco_f0_rmse})
        return new_keys_d

def eval_dur_epoch(model, dloader, epoch_idx, cuda=False,
                   stats=None, va_opts={}, log_writer=None):
    model.eval()
    with torch.no_grad():
        sil_id = 'pau'
        q_classes = False
        if 'sil_id' in va_opts:
            sil_id = va_opts.pop('sil_id')
        if 'q_classes' in va_opts:
            q_classes= va_opts.pop('q_classes')
        idx2spk = None
        if 'idx2spk' in va_opts:
            idx2spk = va_opts.pop('idx2spk')
        if 'mulout' in va_opts:
            print('Multi-Output dur evaluation')
            mulout = va_opts.pop('mulout')
            if idx2spk is None:
                raise ValueError('Specify a idx2spk in eval opts '
                                 'when using MO.')
        assert len(va_opts) == 0, 'unrecognized params passed in: '\
                                  '{}'.format(va_opts.keys())
        spk2durstats=stats
        preds = None
        gtruths = None
        seqlens = None
        spks = None
        # make the silence mask
        sil_mask = None
        for b_idx, batch in enumerate(dloader):
            # decompose the batch into the sub-batches
            spk_b, lab_b, dur_b, slen_b, ph_b = batch
            # build batch of curr_ph to filter out results without sil phones
            # size of curr_ph_b [bsize, seqlen]
            curr_ph_b = [[ph[2] for ph in ph_s] for ph_s in ph_b]
            # convert all into variables and transpose (we want time-major)
            spk_b = spk_b.transpose(0,1)
            lab_b = lab_b.transpose(0,1)
            dur_b = dur_b.transpose(0,1)
            # get curr batch size
            curr_bsz = spk_b.size(1)
            # init hidden states of dur model
            states = model.init_hidden_state(curr_bsz)
            if cuda:
                spk_b = var_to_cuda(spk_b)
                lab_b = var_to_cuda(lab_b)
                dur_b = var_to_cuda(dur_b)
                slen_b = var_to_cuda(slen_b)
                states = var_to_cuda(states)
            # forward through model
            y, states = model(lab_b, states, speaker_idx=spk_b)
            if isinstance(y, dict):
                # we have a MO model, pick the right spk
                spk_name = idx2spk[spk_b.cpu().data[0,0]]
                # print('Extracting y prediction for MO spk ', spk_name)
                y = y[spk_name]
            y = y.squeeze(-1)
            preds, gtruths, \
            spks, sil_mask = predict_masked_rmse(y, dur_b, slen_b, 
                                                 spk_b, curr_ph_b,
                                                 preds, gtruths,
                                                 spks, sil_mask,
                                                 sil_id,
                                                 q_classes)
        # denorm with normalization stats
        assert spk2durstats is not None
        preds, gtruths = denorm_dur_preds_gtruth(preds, gtruths,
                                                 spks, spk2durstats,
                                                 q_classes)
        write_histogram_log(preds, 'eval_preds_rmse',
                            epoch_idx, log_writer)
        write_histogram_log(gtruths, 'eval_gtruths_rmse',
                            epoch_idx, log_writer)
        dur_rmse, spks_rmse = rmse(preds, gtruths, spks)
        dur_rmse *= 1e3
        for k, v in spks_rmse.items():
            spks_rmse[k] = v * 1e3
        nosil_dur_rmse, \
        nosil_spks_rmse = rmse(preds * sil_mask, 
                               gtruths * sil_mask, spks)
        nosil_dur_rmse *= 1e3
        nosil_spkname_rmse = {}
        for k, v in nosil_spks_rmse.items():
            #nosil_spks_rmse[k] = v * 1e3
            nosil_spkname_rmse[idx2spk[int(k)]] = v * 1e3
            write_scalar_log(v * 1e3,
                             'eval_nosil_{}_rmse'.format(idx2spk[int(k)]),
                             epoch_idx, log_writer)
        #print('Evaluated dur mRMSE [ms]: {:.3f}'.format(dur_rmse))
        print('Evaluated dur w/o sil phones mRMSE [ms]:'
              '{:.3f}'.format(nosil_dur_rmse))
        print('Evaluated dur of spks: {}'.format(json.dumps(nosil_spkname_rmse,
                                                            indent=2)))
        nosil_spkname_rmse.update({'eval_total_dur_rmse':dur_rmse,
                                   'eval_total_nosil_dur_rmse':nosil_dur_rmse})
        write_scalar_log(dur_rmse,
                         'eval_total_dur_rmse',
                         epoch_idx, log_writer)
        write_scalar_log(nosil_dur_rmse,
                         'eval_total_nosil_dur_rmse',
                         epoch_idx, log_writer)
        return nosil_spkname_rmse


def train_attaco_epoch(model, dloader, opt, log_freq, epoch_idx,
                       criterion=None, cuda=False, tr_opts={},
                       spk2acostats=None, log_writer=None):
    model.train()
    global_step = epoch_idx * len(dloader)
    # At the moment, acoustic training is always stateful
    spk2acostats = None
    if 'spk2acostats' in tr_opts:
        print('Getting spk2acostats')
        spk2acostats = tr_opts.pop('spk2acostats')
    idx2spk = None
    if 'idx2spk' in tr_opts:
        idx2spk = tr_opts.pop('idx2spk')
    decoder = False
    if 'decoder' in tr_opts:
        decoder = tr_opts.pop('decoder')
    assert len(tr_opts) == 0, 'unrecognized params passed in: '\
                              '{}'.format(tr_opts.keys())
    epoch_losses = {}
    num_batches = len(dloader)
    print('num_batches: ', num_batches)
    pe_start_idx = 0
    for b_idx, batch in enumerate(dloader):
        # decompose the batch into the sub-batches
        spk_b, lab_b, aco_b, slen_b, ph_b = batch
        # build batch of curr_ph to filter out results without sil phones
        # size of curr_ph_b [bsize, seqlen]
        curr_ph_b = [[ph[2] for ph in ph_s] for ph_s in ph_b]
        # transpose (we want time-major)
        spk_b = spk_b.transpose(0,1)
        spk_name = idx2spk[spk_b.data[0,0].item()]
        lab_b = lab_b.transpose(0,1)
        aco_b = aco_b.transpose(0,1)
        aco_p = torch.zeros(1, aco_b.size(1), 
                            aco_b.size(2))
        # get curr batch size
        curr_bsz = spk_b.size(1)
        if cuda:
            spk_b = var_to_cuda(spk_b)
            lab_b = var_to_cuda(lab_b)
            aco_b = var_to_cuda(aco_b)
            aco_p = var_to_cuda(aco_p)
            slen_b = var_to_cuda(slen_b)
        if decoder:
            print('WARNING: decsatt aco does not work well yet'
                  ' cause of real valued feedback problems')
            # forward in teacher force mode the feedback
            # of acoustic features in decoder mode
            fb_aco_b = torch.cat((aco_p,
                                  aco_b[:-1, :, :]), dim=0)
            fb_aco_b = None
            y = model(lab_b, fb_aco_b, speaker_idx=spk_b,
                      pe_start_idx=pe_start_idx)
        else:
            # forward through att encoder model
            y = model(lab_b, speaker_idx=spk_b,
                      pe_start_idx=pe_start_idx)
        y = y.squeeze(-1)
        loss = criterion(y, aco_b)
        preds = None
        gtruths = None
        seqlens = None
        spks = None
        # make the silence mask
        sil_mask = None
        preds, gtruths, \
        spks, sil_mask = predict_masked_mcd(y, aco_b, slen_b, 
                                            spk_b, curr_ph_b,
                                            preds, gtruths,
                                            spks, sil_mask,
                                            'pau')
        assert spk2acostats is not None
        preds, gtruths = denorm_aco_preds_gtruth(preds, gtruths,
                                                 spks, spk2acostats)
        nosil_aco_mcd = mcd(preds[:,:40] * sil_mask, gtruths[:,:40] * sil_mask)
        opt.zero_grad()
        loss.backward()
        opt.step()
        if (b_idx + 1) % log_freq == 0 or \
           (b_idx + 1) >= num_batches:
            log_mesg = 'batch {:4d}/{:4d} '.format(b_idx + 1, num_batches) + \
                       '(pe_start_idx: {:5d}) '.format(pe_start_idx) + \
                       '(epoch {:3d})'.format(epoch_idx)
            log_mesg += ' loss {:.5f}'.format(loss.item())
            if nosil_aco_mcd is not None:
                log_mesg += ', MCD {:.5f} dB'.format(nosil_aco_mcd)
            if 'tr_loss' not in epoch_losses:
                epoch_losses['tr_loss'] = []
                if nosil_aco_mcd:
                    epoch_losses['tr_mcd'] = []
            epoch_losses['tr_loss'].append(loss.item())
            write_scalar_log(loss.item(), 'tr_loss',
                             global_step, log_writer)
            if nosil_aco_mcd:
                epoch_losses['tr_mcd'].append(nosil_aco_mcd)
                write_scalar_log(nosil_aco_mcd, 'tr_mcd',
                                 global_step, log_writer)
            print(log_mesg)
        global_step += 1
        pe_start_idx += aco_b.size(0)
    end_log = '-- Finished epoch {:4d}, mean losses:'.format(epoch_idx)
    if isinstance(epoch_losses, dict):
        for k, val in epoch_losses.items():
            end_log += ' ({} : {:.5f})'.format(k, np.mean(val))
    end_log += ' --'
    print(end_log)
    return epoch_losses

def eval_attaco_epoch(model, dloader, epoch_idx, cuda=False,
                      stats=None, va_opts={}, log_writer=None,
                      reset_batch_state=False):
    model.eval()
    with torch.no_grad():
        sil_id = 'pau'
        if 'sil_id' in va_opts:
            sil_id = va_opts.pop('sil_id')
        idx2spk = None
        if 'idx2spk' in va_opts:
            idx2spk = va_opts.pop('idx2spk')
        decoder = False
        if 'decoder' in va_opts:
            decoder = va_opts.pop('decoder')
        assert len(va_opts) == 0, 'unrecognized params passed in: '\
                                  '{}'.format(va_opts.keys())
        spk2acostats=stats
        preds = None
        gtruths = None
        seqlens = None
        spks = None
        # make the silence mask
        sil_mask = None
        all_phones = []
        pe_start_idx = 0
        for b_idx, batch in enumerate(dloader):
            # decompose the batch into the sub-batches
            spk_b, lab_b, aco_b, slen_b, ph_b = batch
            # build batch of curr_ph to filter out results without sil phones
            # size of curr_ph_b [bsize, seqlen]
            curr_ph_b = []
            for ph_s in ph_b:
                phone_seq = []
                for ph in ph_s:
                    phone_seq.append(ph[2])
                    all_phones.append(ph[2])
                curr_ph_b.append(phone_seq)
            # transpose (we want time-major)
            spk_b = spk_b.transpose(0,1)
            spk_name = idx2spk[spk_b.cpu().data[0,0].item()]
            lab_b = lab_b.transpose(0,1)
            aco_b = aco_b.transpose(0,1)
            aco_p = torch.zeros(1, aco_b.size(1), 
                                aco_b.size(2))
            # get curr batch size
            curr_bsz = spk_b.size(1)
            if cuda:
                spk_b = var_to_cuda(spk_b)
                lab_b = var_to_cuda(lab_b)
                aco_b = var_to_cuda(aco_b)
                aco_p = var_to_cuda(aco_p)
                slen_b = var_to_cuda(slen_b)
            if decoder:
                # forward in teacher force mode the feedback
                # of acoustic features in decoder mode
                fb_aco_b = torch.cat((aco_p,
                                      aco_b[:-1, :, :]), dim=0)
                fb_aco_b = None
                y = model(lab_b, fb_aco_b, speaker_idx=spk_b,
                          pe_start_idx=pe_start_idx)
            else:
                # forward through att encoder model
                y = model(lab_b, speaker_idx=spk_b,
                          pe_start_idx=pe_start_idx)
            if not reset_batch_state:
                pe_start_idx += aco_b.size(0)
            y = y.cpu()
            spk_npy = spk_b.cpu().data.numpy()
            all_comp = np.all(spk_npy == spk_npy[0, 0]), spk_npy
            assert all_comp
            preds, gtruths, \
            spks, sil_mask = predict_masked_mcd(y, aco_b, slen_b, 
                                                spk_b, curr_ph_b,
                                                preds, gtruths,
                                                spks, sil_mask,
                                                sil_id)
        # denorm with normalization stats
        assert spk2acostats is not None
        preds, gtruths = denorm_aco_preds_gtruth(preds, gtruths,
                                                 spks, spk2acostats)
        aco_mcd = mcd(preds[:,:40], gtruths[:,:40], spks, idx2spk)
        aco_afpr = afpr(np.round(preds[:,-1]).reshape(-1, 1),
                        gtruths[:,-1].reshape(-1, 1), spks, 
                        idx2spk)
        aco_f0_rmse, aco_f0_spk = rmse(np.exp(preds[:, -2]), 
                                       np.exp(gtruths[:, -2]),
                                       spks, idx2spk)
        masked_f0_preds = np.exp(preds[:, -2]).reshape(-1, 1) * sil_mask
        masked_f0_gtruths = np.exp(gtruths[:, -2]).reshape(-1, 1) * sil_mask
        write_histogram_log(np.exp(preds[:, -2]),
                            'F0 predictions',
                            epoch_idx, log_writer)
        write_histogram_log(np.exp(gtruths[:, -2]),
                            'F0 groundtruth',
                            epoch_idx, log_writer)
        nosil_aco_f0_rmse, \
        nosil_aco_f0_spk = rmse(masked_f0_preds,
                                masked_f0_gtruths,
                                spks, idx2spk)
        write_histogram_log(preds[:, :40],
                            'MFCC predictions',
                            epoch_idx, log_writer)
        write_histogram_log(gtruths[:, :40],
                            'MFCC groundtruth',
                            epoch_idx, log_writer)
        masked_cc_preds = preds[:, :40] * sil_mask
        masked_cc_gtruths = gtruths[:, :40] * sil_mask
        nosil_aco_mcd = mcd(masked_cc_preds, masked_cc_gtruths,
                            spks, idx2spk)
        write_histogram_log(preds[:, -1],
                            'U/V predictions',
                            epoch_idx, log_writer)
        write_histogram_log(gtruths[:, -1],
                            'U/V groundtruth',
                            epoch_idx, log_writer)
        masked_uv_preds = np.round(preds[:, -1]).reshape(-1, 1) * sil_mask
        masked_uv_gtruths = gtruths[:, -1].reshape(-1, 1) * sil_mask
        write_histogram_log(preds[:, -3],
                            'FV predictions',
                            epoch_idx, log_writer)
        write_histogram_log(gtruths[:, -3],
                            'FV groundtruth',
                            epoch_idx, log_writer)
        nosil_aco_afpr = afpr(masked_uv_preds, masked_uv_gtruths,
                              spks, idx2spk)

        #print('Evaluated aco MCD [dB]: {:.3f}'.format(aco_mcd['total']))
        print('========= F0 RMSE =========')
        print('Evaluated aco W/O sil phones ({}) F0 mRMSE [Hz]:'
              '{:.2f}'.format(sil_id, nosil_aco_f0_rmse))
        write_scalar_log(nosil_aco_f0_rmse, 
                         'total_no-silence_F0_rmse_Hz',
                         epoch_idx, log_writer)
        print('Evaluated aco F0 mRMSE of spks: '
              '{}'.format(json.dumps(nosil_aco_f0_spk,
                                     indent=2)))
        if len(nosil_aco_f0_spk) > 1:
            for k, v in nosil_aco_f0_spk.items():
                write_scalar_log(v, '{}_no-silence_F0_rmse_Hz'.format(k),
                                 epoch_idx, log_writer)
        print('========= MCD =========')
        print('Evaluated aco W/O sil phones ({}) MCD [dB]:'
              '{:.3f}'.format(sil_id, nosil_aco_mcd['total']))
        write_scalar_log(nosil_aco_mcd['total'],
                         'total_MCD_dB',
                         epoch_idx, log_writer)
        #print('Evaluated w/ sil MCD of spks: {}'.format(json.dumps(aco_mcd,
        #                                                           indent=2)))
        print('Evaluated W/O sil MCD of spks: {}'.format(json.dumps(nosil_aco_mcd,
                                                                    indent=2)))
        if len(nosil_aco_mcd) > 2:
            # will print all speakers
            for k, v in nosil_aco_mcd.items():
                if k == 'total':
                    continue
                write_scalar_log(v,
                                 'MCD_spk{}_dB'.format(k),
                                 epoch_idx, log_writer)
        print('========= Acc =========')
        #print('Evaluated aco AFPR [norm]: '.format(aco_afpr['A.total']))
        print('Evaluated W/O sil phones ({}) Acc [norm]:'
              '{}'.format(sil_id, nosil_aco_afpr['A.total']))
        write_scalar_log(nosil_aco_afpr['A.total'], 'Total Accuracy',
                         epoch_idx, log_writer)
        print('Evaluated W/O sil phones ({}) P [norm]:'
              '{}'.format(sil_id, nosil_aco_afpr['P.total']))
        write_scalar_log(nosil_aco_afpr['P.total'], 'Total Precision',
                         epoch_idx, log_writer)
        print('Evaluated W/O sil phones ({}) R [norm]:'
              '{}'.format(sil_id, nosil_aco_afpr['R.total']))
        write_scalar_log(nosil_aco_afpr['R.total'], 'Total Recall',
                         epoch_idx, log_writer)
        print('Evaluated W/O sil phones ({}) F1 [norm]:'
              '{}'.format(sil_id, nosil_aco_afpr['F.total']))
        write_scalar_log(nosil_aco_afpr['F.total'], 'total F1',
                         epoch_idx, log_writer)
        print('=' * 30)
        # WRITE AUDIO TO TBOARD 
        if log_writer is not None:
            tfl = tempfile.NamedTemporaryFile()
            cc = preds[:, :40]
            fv = preds[:, -3]
            lf0 = preds[:, -2]
            write_aco_file('{}.cc'.format(tfl.name), cc)
            write_aco_file('{}.fv'.format(tfl.name), fv)
            write_aco_file('{}.lf0'.format(tfl.name), lf0)
            aco2wav('{}'.format(tfl.name))
            rate, wav = wavfile.read('{}.wav'.format(tfl.name))
            # norm in wav
            wav = np.array(wav, dtype=np.float32) / 32767.
            # trim to max of 10 seconds
            wav = wav[:min(wav.shape[0], int(rate * 10))]
            log_writer.add_audio('eval_synth_audio',
                                 wav,
                                 epoch_idx,
                                 sample_rate=rate)
            # remove tmp files
            os.unlink('{}.cc'.format(tfl.name))
            os.unlink('{}.fv'.format(tfl.name))
            os.unlink('{}.lf0'.format(tfl.name))
            os.unlink('{}.wav'.format(tfl.name))
        # transform nosil_aco_mcd keys
        new_keys_d = {}
        for k in nosil_aco_mcd.keys():
            if k == 'total':
                # skip this key
                continue
            # transform each key into the desired loss filename 
            new_keys_d['so-{}_va_mcd'.format(k)] = nosil_aco_mcd[k]
        for k in nosil_aco_afpr.keys():
            if k == 'total':
                continue
            new_keys_d['so-{}_va_afpr'.format(k)] = nosil_aco_afpr[k]
        for k in nosil_aco_f0_spk.keys():
            new_keys_d['so-{}_va_f0rmse'.format(k)] = nosil_aco_f0_spk[k]
        new_keys_d.update({'total_aco_mcd':aco_mcd['total'],
                           'total_nosil_aco_mcd':nosil_aco_mcd['total'],
                           'total_aco_afpr':aco_afpr['total'],
                           'total_nosil_aco_afpr':nosil_aco_afpr['total'],
                           'total_aco_f0rmse':aco_f0_rmse,
                           'total_nosil_aco_f0rmse':nosil_aco_f0_rmse})
        return new_keys_d

