from __future__ import division
import os
import numpy as np
import random
import dynet as dy
import sys
import json
import macarico.util
from collections import Counter
import pickle
import glob
import itertools

macarico.util.reseed()

import macarico.lts.lols
reload(macarico.lts.lols)

from macarico.data import nlp_data
from macarico.annealing import ExponentialAnnealing, NoAnnealing, stochastic, EWMA
from macarico.lts.aggrevate import AggreVaTe
from macarico.lts.lols import BanditLOLS, BanditLOLSMultiDev#, BanditLOLSRewind
from macarico.tasks.sequence_labeler import Example, HammingLoss, HammingLossReference
from macarico.tasks.seq2seq import EditDistance, EditDistanceReference
from macarico.features.sequence import RNNFeatures, BOWFeatures, AttendAt, DilatedCNNFeatures
from macarico.features.actor import TransitionRNN, TransitionBOW
from macarico.policies.bootstrap import BootstrapPolicy
from macarico.policies.linear import LinearPolicy
from macarico.policies.active import CSActive
from macarico.lts.dagger import DAgger
from macarico.lts.reinforce import Reinforce, AdvantageActorCritic, LinearValueFn
from macarico.lts.ppo import PPO
from macarico.tasks.dependency_parser import DependencyAttention, AttachmentLoss, AttachmentLossReference
from macarico.tasks.gridworld import GlobalGridFeatures, LocalGridFeatures, make_default_gridworld, GridLoss
from macarico.tasks.pendulum import Pendulum, PendulumLoss, PendulumFeatures
from macarico.tasks.blackjack import Blackjack, BlackjackLoss, BlackjackFeatures
from macarico.tasks.mountain_car import MountainCar, MountainCarLoss, MountainCarFeatures
from macarico.tasks.hexgame import Hex, HexLoss, HexFeatures
from macarico.tasks.cartpole import CartPoleEnv, CartPoleLoss, CartPoleFeatures
from macarico.tasks.pocman import MicroPOCMAN, MiniPOCMAN, FullPOCMAN, POCLoss, LocalPOCFeatures, POCReference
from macarico.tasks import dependency_parser

names = 'blols_1 blols_2 blols_3 blols_4 blols_1_learn blols_2_learn blols_3_learn blols_4_learn blols_1_bl blols_3_bl blols_4_bl blols_1_pref blols_2_pref blols_3_pref blols_4_pref blols_1_pref_os blols_2_pref_os blols_3_pref_os blols_4_pref_os blols_1_pref_learn blols_2_pref_learn blols_3_pref_learn blols_4_pref_learn blols_1_pref_learn_os blols_2_pref_learn_os blols_3_pref_learn_os blols_4_pref_learn_os reinforce reinforce_nobl reinforce_md1 reinforce_uni reinforce_md1_uni reinforce_md1_nobl reinforce_uni_nobl reinforce_md1_uni_nobl'.split()

def dumpit():
    pickle.dump([(name, globals()[name]) for name in names if name in globals()], open('big_banditlols.new.saved','w'))

##############################################################################
## SETUP UP DATASETS
##############################################################################

def merge_vocab(v1, v2, outfile):
    v3 = {}
    for k, v in v1.iteritems():
        v3[k] = v
    for k, v in v2.iteritems():
        if k in v3: continue
        v3[k] = len(v3)
    with open(outfile, 'w') as h:
        for _, w in sorted(((v, k) for k, v in v3.iteritems())):
            print >>h, w

DATA_DIR = os.environ.get('MACARICO_DATA', '.')
if DATA_DIR[-1] != '/': DATA_DIR += '/'
def has_data(path): return os.path.isdir(path + 'data') and os.path.isdir(path + 'bandit_data')
if not has_data(DATA_DIR):
    print >>sys.stderr, 'warning: %s does not contain data, trying alternatives' % DATA_DIR
    success = False
    dirs = ['./', '/bscratch/hal3/', '/cliphomes/hal/projects/macarico/tests/', '/home/hal/projects/macarico/tests/', '/home/hal3/bscratch/', 'data/']
    for d in dirs:
        if has_data(d):
            DATA_DIR = d
            success = True
            break
    if success:
        print >>sys.stderr, 'success: using %s' % DATA_DIR
    else:
        print >>sys.stderr, 'failure, continuing anyway and crossing fingers'

def do_merge():
    _,_,_,t1,p1,_ = nlp_data.read_wsj_deppar(DATA_DIR + 'bandit_data/dep_parsing/dep_wsj.mac', n_tr=9999999, min_freq=5)
    _,_,_,t2,p2,_ = nlp_data.read_wsj_deppar(DATA_DIR + 'bandit_data/dep_parsing/dep_tweebank.mac', n_tr=9999999, min_freq=3)
    merge_vocab(t1, t2, DATA_DIR + 'bandit_data/dep_parsing/vocab.tok')
    merge_vocab(p1, p2, DATA_DIR + 'bandit_data/dep_parsing/vocab.pos')

    _,_,_,t1,_ = nlp_data.read_wsj_pos(DATA_DIR + 'bandit_data/pos/pos_wsj.mac', n_tr=9999999, min_freq=5)
    _,_,_,t2,_ = nlp_data.read_wsj_pos(DATA_DIR + 'bandit_data/pos/pos_tweebank.mac', n_tr=9999999, min_freq=3)
    merge_vocab(t1, t2, DATA_DIR + 'bandit_data/pos/vocab.tok')

    _,_,_,t1,_ = nlp_data.read_wsj_pos(DATA_DIR + 'bandit_data/chunking/chunk_train.mac', n_tr=9999999, min_freq=5)
    _,_,_,t2,_ = nlp_data.read_wsj_pos(DATA_DIR + 'bandit_data/chunking/chunk_test.mac', n_tr=9999999, min_freq=3)
    merge_vocab(t1, t2, DATA_DIR + 'bandit_data/chunking/vocab.tok')

    _,_,_,t1,_ = nlp_data.read_wsj_pos(DATA_DIR + 'bandit_data/ctb/nw.mac', n_tr=9999999, min_freq=5)
    _,_,_,t2,_ = nlp_data.read_wsj_pos(DATA_DIR + 'bandit_data/ctb/sc.mac', n_tr=9999999, min_freq=5)
    merge_vocab(t1, t2, DATA_DIR + 'bandit_data/ctb/vocab.tok')

def read_vocab(filename):
    v = {}
    for l in open(filename):
        v[l.strip()] = len(v)
    return v

def attach_two_trees(t1, t2):
    n = len(t1.tokens)
    m = len(t2.tokens)
    root = n+m
    return dependency_parser.Example(
        t1.tokens + t2.tokens,
        [root if h==n else h   for h in t1.heads] +
        [root if h==m else h+n for h in t2.heads],
        (t1.rels + t2.rels) if t1.rels is not None and t2.rels is not None else None,
        max(t1.n_rels, t2.n_rels),
        t1.pos + t2.pos)

def setup_mod(dy_model, n_train=50, n_de=100, n_types=10, n_labels=4, length=6):
    data = macarico.util.make_sequence_mod_data(n_train+n_de, length, n_types, n_labels)
    data = [Example(x, y, n_labels) for x, y in data]
    train = data[n_de:]
    dev = data[:n_de]
    attention = lambda features: [AttendAt(field=f.field) for f in features]
    reference = HammingLossReference()
    losses = [HammingLoss()]
    mk_feats = lambda fb, oid: [fb(dy_model, n_types, output_id=oid)]
    return train, dev, attention, reference, losses, mk_feats, n_labels, None


def setup_gridworld(dy_model,
                    n_tr=32768,
                    n_de=100,
                    per_step_cost=0.05,
                    p_step_success=0.9,
                    ):
    data = [make_default_gridworld(p_step_success=p_step_success, start_random=True, per_step_cost=per_step_cost) for _ in xrange(n_tr+n_de)]
    train, dev = data[:n_tr], data[n_tr:_]
    attention = lambda _: [AttendAt(lambda _: 0, 'grid')]
    mk_feats = lambda fb, oid: [fb(dy_model, None, output_id=oid)]
    return train, dev, attention, None, [GridLoss()], mk_feats, 4, None

def setup_pendulum(dy_model, n_tr=1024, n_de=100):
    data = [Pendulum() for _ in xrange(n_tr+n_de)]
    attention = lambda _: [AttendAt(lambda _: 0, 'pendulum')]
    mk_feats = lambda fb, oid: [fb(dy_model, None, output_id=oid)]
    return data[:n_tr], data[n_tr:], attention, None, [PendulumLoss()], mk_feats, data[0].n_actions, None
    
def setup_blackjack(dy_model, n_tr=1024, n_de=100):
    data = [Blackjack() for _ in xrange(n_tr+n_de)]
    attention = lambda _: [AttendAt(lambda _: 0, 'blackjack')]
    mk_feats = lambda fb, oid: [fb(dy_model, None, output_id=oid)]
    return data[:n_tr], data[n_tr:], attention, None, [BlackjackLoss()], mk_feats, data[0].n_actions, None
    
def setup_hex(dy_model, n_tr=1024, n_de=100, board_size=5):
    data = [Hex(np.random.randint(0,2), board_size) for _ in xrange(n_tr+n_de)]
    attention = lambda _: [AttendAt(lambda _: 0, 'hex')]
    mk_feats = lambda fb, oid: [fb(dy_model, None, output_id=oid)]
    return data[:n_tr], data[n_tr:], attention, None, [HexLoss()], mk_feats, data[0].n_actions, None
    
def setup_mountaincar(dy_model, n_tr=1024, n_de=100):
    data = [MountainCar() for _ in xrange(n_tr+n_de)]
    attention = lambda _: [AttendAt(lambda _: 0, 'mountain_car')]
    mk_feats = lambda fb, oid: [fb(dy_model, None, output_id=oid)]
    return data[:n_tr], data[n_tr:], attention, None, [MountainCarLoss()], mk_feats, data[0].n_actions, None

def setup_cartpole(dy_model, n_tr=1024, n_de=100):
    data = [CartPoleEnv() for _ in xrange(n_tr+n_de)]
    attention = lambda _: [AttendAt(lambda _: 0, 'cartpole')]
    mk_feats = lambda fb, oid: [fb(dy_model, None, output_id=oid)]
    return data[:n_tr], data[n_tr:], attention, None, [CartPoleLoss()], mk_feats, data[0].n_actions, None
    
def setup_pocman(dy_model, n_tr, n_de, size='micro', ref='ref'):
    MyPOCMAN = MicroPOCMAN if size == 'micro' else \
               MiniPOCMAN  if size == 'mini'  else \
               FullPOCMAN  if size == 'full'  else \
               None
    data = [MyPOCMAN() for _ in xrange(n_tr+n_de)]
    attention = lambda _: [AttendAt(lambda _: 0, 'poc')]
    mk_feats = lambda fb, oid: [fb(dy_model, None, output_id=oid)]
    reference = POCReference() if ref == 'ref' else None
    return data[:n_tr], data[n_tr:], attention, reference, [CartPoleLoss()], mk_feats, data[0].n_actions, None

def setup_sequence(dy_model, filename, n_train, n_de, use_token_vocab=None, tag_vocab=None):
    USE_BOW_TOO = False
    train, dev, test, token_vocab, label_id = nlp_data.read_wsj_pos(filename, n_tr=n_train, n_de=n_de, n_te=0, min_freq=1, use_token_vocab=use_token_vocab, use_tag_vocab=tag_vocab)
    attention = lambda features: [AttendAt(field=f.field) for f in features]
    reference = HammingLossReference()
    losses = [HammingLoss()]
    n_labels = len(label_id)
    n_types = len(token_vocab)
    mk_feats = lambda fb, oid: [fb(dy_model, n_types, use_word_embeddings=True, output_id=oid)]
    if USE_BOW_TOO:
        mk_feats0 = mk_feats
        mk_feats = lambda fb, oid: mk_feats0(fb, oid) + [BOWFeatures(dy_model, n_types, output_field='tokens_bow' + oid)]
        assert False
    return train, dev, attention, reference, losses, mk_feats, n_labels, token_vocab

def setup_deppar(dy_model, filename, n_train, n_de, use_token_vocab=None, use_pos_vocab=None, attach_trees=False):
    train, dev, test, token_vocab, pos_vocab, rel_id = nlp_data.read_wsj_deppar(filename, n_tr=n_train, n_de=n_de, n_te=0, min_freq=2, use_token_vocab=use_token_vocab, use_pos_vocab=use_pos_vocab)
    if attach_trees:
        n = len(train)
        for i in xrange(0, n, 2):
            train.append(attach_two_trees(train[i], train[i+1]))
        random.shuffle(train)
    attention = lambda _: [DependencyAttention(),
                           DependencyAttention(field='pos_rnn')]
    reference = AttachmentLossReference()
    losses = [AttachmentLoss()]
    n_types = len(token_vocab)
    n_pos = len(pos_vocab)
    n_labels = 3 + len(rel_id)
    mk_feats = lambda fb, oid: [fb(dy_model, n_types, use_word_embeddings=True),
                                fb(dy_model, n_pos, input_field='pos', output_field='pos_rnn')]
    return train, dev, attention, reference, losses, mk_feats, n_labels, token_vocab

def setup_translit(dy_model, filename, n_de):
    [filename_src, filename_tgt] = filename.split(':')
    train, dev, src_voc, tgt_voc = nlp_data.read_parallel_data(filename_src, filename_tgt, n_de=n_de, min_src_freq=2, shuffle=True)
    attention = lambda features: [SoftmaxAttention(dy_model, features, 50)]
    n_types = len(src_voc)
    n_labels = len(tgt_voc)
    reference = EditDistanceReference()
    losses = [EditDistance()]
    mk_feats = lambda fb: [fb(dy_model, n_types)]
    return train, dev, attention, reference, losses, mk_feats, n_labels, src_voc
    

##############################################################################
## SETUP UP LEARNING ALGORITHMS
##############################################################################

def setup_banditlols(dy_model, learning_method):
    if dy_model is None:
        return [['ips', 'dr', 'mtr'],
                ['uniform', 'boltzmann'],
                ['upc', ''],
                ['oft', 'multidev', ''],
                ['explore=1.0', 'explore=0.5', 'explore=0.1', 'annealexp::explore=0.99999'],
                ['p_rin=0.0', 'p_rin=0.99999', 'p_rin=1.0'],
                ['p_rout=0.0', 'p_rout=0.5', 'p_rout=1.0'],
                ['temp=0.2', 'temp=1', 'temp=2'],
                ]

    learning_method = learning_method.split('::')
    update_method = \
      BanditLOLS.LEARN_IPS    if 'ips'    in learning_method else \
      BanditLOLS.LEARN_BIASED if 'biased' in learning_method else \
      BanditLOLS.LEARN_DR     if 'dr'     in learning_method else \
      BanditLOLS.LEARN_MTR    if 'mtr'    in learning_method else \
      BanditLOLS.LEARN_MTR_ADVANTAGE if 'mtra' in learning_method else \
      None
    exploration_method = \
      BanditLOLS.EXPLORE_UNIFORM if 'uniform' in learning_method else \
      BanditLOLS.EXPLORE_BOLTZMANN if 'boltzmann' in learning_method else \
      BanditLOLS.EXPLORE_BOLTZMANN_BIASED if 'biasedboltz' in learning_method else \
      BanditLOLS.EXPLORE_BOOTSTRAP if 'bootstrap' in learning_method else \
      None
    temperature = 1.0
    use_prefix_costs = 'upc' in learning_method
    offset_t = 'oft' in learning_method
    p_rin = 0.
    p_rout = 0.
    explore = 1.
    for x in learning_method:
        if   x.startswith('p_rin='): p_rin = float(x[6:])
        elif x.startswith('p_rout='): p_rout = float(x[7:])
        elif x.startswith('temp='): temperature = float(x[5:])
        elif x.startswith('explore='): explore = float(x[8:])
        #else: assert '=' not in x, 'unknown arg: ' + x

    p_rollin_ref  = stochastic(ExponentialAnnealing(p_rin))
    p_rollout_ref = stochastic(NoAnnealing(p_rout))
    run_per_batch = [p_rollout_ref.step, p_rollin_ref.step]
    if 'annealexp' in learning_method:
        explore = stochastic(ExponentialAnnealing(explore))
        run_per_batch += [explore.step]

    BLOLS = BanditLOLSMultiDev if 'multidev' in learning_method else \
            BanditLOLS
    builder = (lambda reference, policy: \
               BanditLOLSMultiDev(reference, policy, p_rollin_ref, p_rollout_ref,
                                  update_method, exploration_method,
                                  temperature=temperature,
                                  use_prefix_costs=use_prefix_costs, explore=explore,
                                  offset_t=offset_t, no_certainty_tracker=not offset_t)
               ) if 'multidev' in learning_method else \
              (lambda reference, policy: \
               BanditLOLS(reference, policy, p_rollin_ref, p_rollout_ref,
                          update_method, exploration_method,
                          temperature=temperature,
                          use_prefix_costs=use_prefix_costs, explore=explore,
                          offset_t=offset_t)
               )
    return builder, run_per_batch

def setup_reinforce(dy_model, learning_method):
    if dy_model is None:
        return [['baseline=0.0', 'baseline=0.5', 'baseline=0.8'],
                ['maxd=1', '']]

    learning_method = learning_method.split('::')
    baseline = 0.8
    max_deviations = None
    for x in learning_method:
        if   x.startswith('baseline='): baseline = float(x[9:])
        elif x.startswith('maxd='): max_deviations = int(x[5:])
        else: assert '=' not in x, 'unknown arg: ' + x
    baseline = EWMA(baseline)
    return lambda _, policy: \
        Reinforce(policy, baseline, max_deviations=max_deviations), \
        []

def setup_aac(dy_model, learning_method, dim):
    if dy_model is None:
        return []

    lvf = LinearValueFn(dy_model, dim)
    learning_method = learning_method.split('::')
    vfa_multiplier = 1.0
    for x in learning_method:
        if x.startswith('mult='): vfa_multiplier = float(x[5:])

    #def builder(reference, policy):
        #baseline = LinearValueFn(dy_model, policy.features.dim)
        #policy.vfa = baseline
        #baseline = None
        #return AdvantageActorCritic(policy, baseline)
    return lambda _, policy: \
        AdvantageActorCritic(policy, lvf, vfa_multiplier=vfa_multiplier), \
        []
    
        
    #return builder, []

def setup_ppo(dy_model, learning_method):
    if dy_model is None:
        return [['baseline=0.0', 'baseline=0.5', 'baseline=0.8'],
                ['epsilon=%g' % e for e in [0.01, 0.05, 0.1, 0.2, 0.4, 0.8]]]

    learning_method = learning_method.split('::')
    baseline = 0.8
    epsilon = 0.1
    for x in learning_method:
        if   x.startswith('baseline='): baseline = float(x[9:])
        elif x.startswith('epsilon='): epsilon = float(x[8:])
        else: assert '=' not in x, 'unknown arg: ' + x
    baseline = EWMA(baseline)
    return lambda _, policy: \
        PPO(policy, baseline, epsilon), \
        []

def setup_dagger(dy_model, learning_method):
    if dy_model is None:
        return [['p_rin=0.0', 'p_rin=0.999', 'p_rin=0.99999', 'p_rin=1.0']]

    learning_method = learning_method.split('::')
    p_rin = 0.
    for x in learning_method:
        if x.startswith('p_rin='): p_rin = float(x[6:])
        else: assert '=' not in x, 'unknown arg: ' + x
    p_rollin_ref  = stochastic(ExponentialAnnealing(p_rin))
    return lambda reference, policy: \
        DAgger(reference, policy, p_rollin_ref), \
        [p_rollin_ref.step]

def setup_aggrevate(dy_model, learning_method):
    if dy_model is None:
        return [['p_rin=0.0', 'p_rin=0.999', 'p_rin=0.99999', 'p_rin=1.0']]

    learning_method = learning_method.split('::')
    p_rin = 0.
    for x in learning_method:
        if x.startswith('p_rin='): p_rin = float(x[6:])
        else: assert '=' not in x, 'unknown arg: ' + x
    p_rollin_ref  = stochastic(ExponentialAnnealing(p_rin))
    return lambda reference, policy: \
        AggreVaTe(reference, policy, p_rollin_ref), \
        [p_rollin_ref.step]


##############################################################################
## RUN EXPERIMENTS
##############################################################################

def split_sequences(data, maxlength):
    def split_one(ex):
        for st in xrange(0, len(ex.tokens), maxlength):
            yield Example(ex.tokens[st:st+maxlength],
                          ex.labels[st:st+maxlength],
                          ex.n_labels)
    new_data = []
    for ex in data:
        for ex2 in split_one(ex):
            new_data.append(ex2)
    return new_data

#def test1(learning_method, exploration, N=50, n_types=10, n_labels=4, length=6, random_seed=20001, bow=True, method='banditlols', temperature=1, p_ref=1, baseline=0.8, uniform=False, max_deviations=None, use_prefix_costs=False, epsilon=1.0, offset_t=False, learning_rate=0.001, loss_fn='squared', task='mod'):
def run(task='mod::160::4::20', \
        learning_method='blols::dr::boltzmann::upc::oft::multidev::explore=0',
        opt_method='adam',
        learning_rate=0.001,
        seqfeats='rnn',
        active=False,
        supervised=False,
        initial_embeddings=None,
        save_best_model_to=None,
        load_initial_model_from=None,
        token_vocab_file=None,
        pos_vocab_file=None,
        additional_args=[],
       ):
    print >>sys.stderr, ''
    #print >>sys.stderr, '# testing learning_method=%d exploration=%d' % (learning_method, exploration)
    print >>sys.stderr, '# %s' % locals()
    print >>sys.stderr, ''

    dy_model = dy.ParameterCollection()

    # hack for easy tasks
    tag_list = None
    if task == 'pos-wsj':
        task = 'seq::' + DATA_DIR + 'bandit_data/pos/pos_wsj.mac::40000::2248'
        token_vocab_file = DATA_DIR + 'bandit_data/pos/vocab.tok'
        tag_list = '1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 43 44 45'
    elif task == 'pos-tweet':
        task = 'seq::' + DATA_DIR + 'bandit_data/pos/pos_tweebank.mac::800::129'
        token_vocab_file = DATA_DIR + 'bandit_data/pos/vocab.tok'
        tag_list = '1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 43 44 45'
    elif task == 'chunk-train':
        task = 'seq::' + DATA_DIR + 'bandit_data/chunking/chunk_train.mac::8000::936'
        token_vocab_file = DATA_DIR + 'bandit_data/chunking/vocab.tok'
        tag_list = '1 2 3'
    elif task == 'chunk-test':
        task = 'seq::' + DATA_DIR + 'bandit_data/chunking/chunk_test.mac::1800::212'
        token_vocab_file = DATA_DIR + 'bandit_data/chunking/vocab.tok'
        tag_list = '1 2 3'
    elif task == 'dep-wsj':
        task = 'dep::' + DATA_DIR + 'bandit_data/dep_parsing/dep_wsj.mac::40000::2245'
        token_vocab_file = DATA_DIR + 'bandit_data/dep_parsing/vocab.tok'
        pos_vocab_file = DATA_DIR + 'bandit_data/dep_parsing/vocab.pos'
    elif task == 'dep-tweet':
        task = 'dep::' + DATA_DIR + 'bandit_data/dep_parsing/dep_tweebank.mac::800::129'
        token_vocab_file = DATA_DIR + 'bandit_data/dep_parsing/vocab.tok'
        pos_vocab_file = DATA_DIR + 'bandit_data/dep_parsing/vocab.pos'
    elif task == 'ctb-nw':
        task = 'seq::' + DATA_DIR + 'bandit_data/ctb/nw.mac::9000::1650'
        token_vocab_file = DATA_DIR + 'bandit_data/ctb/vocab.tok'
        tag_list = 'AD AS BA CC CD CS DEC DEG DER DEV DT EM ETC FW IJ JJ LB LC M MSP NN NN-SHORT NOI NR NR-SHORT NT NT-SHORT OD ON P PN PU SB SP URL VA VC VE VV'
    elif task == 'ctb-sc':
        task = 'seq::' + DATA_DIR + 'bandit_data/ctb/sc.mac::38000::1927'
        token_vocab_file = DATA_DIR + 'bandit_data/ctb/vocab.tok'
        tag_list = 'AD AS BA CC CD CS DEC DEG DER DEV DT EM ETC FW IJ JJ LB LC M MSP NN NN-SHORT NOI NR NR-SHORT NT NT-SHORT OD ON P PN PU SB SP URL VA VC VE VV'
    elif task.startswith('grid'):
        if task == 'grid':
            task = 'grid::0.05::0.9'
        seqfeats = 'grid'
    elif task == 'pendulum':
        seqfeats = 'pendulum'
    elif task == 'blackjack':
        seqfeats = 'blackjack'
    elif task.startswith('hex'):
        if task == 'hex':
            task = 'hex::3'
        seqfeats = 'hex'
    elif task == 'mountaincar':
        seqfeats = 'mountaincar'
    elif task == 'cartpole':
        seqfeats = 'cartpole'
    elif task.startswith('pocman'):
        if task == 'pocman':
            task = 'pocman::micro::ref'
        seqfeats = 'pocman'

    if initial_embeddings == 'yes' or initial_embeddings == '50':
        initial_embeddings = (DATA_DIR + 'data/wiki.zh.vec50.gz') if 'ctb' in task else \
                             (DATA_DIR + 'data/glove.6B.50d.txt.gz')

    if initial_embeddings == '100':
        initial_embeddings = (DATA_DIR + 'data/glove.6B.100d.txt.gz') if 'ctb' not in task else None
        
    if initial_embeddings == '200':
        initial_embeddings = (DATA_DIR + 'data/glove.6B.200d.txt.gz') if 'ctb' not in task else None
        
    if initial_embeddings == '300':
        initial_embeddings = (DATA_DIR + 'data/wiki.zh.vec.gz') if 'ctb' in task else \
                             (DATA_DIR + 'data/glove.6B.300d.txt.gz')
    task_args = task.split('::')
    task = task_args[0]
    task_args = task_args[1:]


    # TODO if we pretrain, be intelligent about vocab
    token_vocab = None if token_vocab_file is None else read_vocab(token_vocab_file)
    pos_vocab = None if pos_vocab_file is None else read_vocab(pos_vocab_file)

    tag_vocab = None
    if tag_list is not None:
        tag_vocab = {}
        for s in tag_list.split():
            tag_vocab[s] = len(tag_vocab)
    
    train, dev, attention, reference, losses, mk_feats, n_labels, word_vocab = \
      setup_mod(dy_model, 65536, 100, int(task_args[0]), int(task_args[1]), int(task_args[2])) if task == 'mod' else \
      setup_sequence(dy_model, task_args[0], int(task_args[1]), int(task_args[2]), token_vocab, tag_vocab) if task == 'seq' else \
      setup_deppar(dy_model, task_args[0], int(task_args[1]), int(task_args[2]), token_vocab, pos_vocab, False) if task == 'dep' else \
      setup_translit(dy_model, task_args[0], int(task_args[1])) if task == 'trn' else \
      setup_gridworld(dy_model, 2**14, 100, float(task_args[0]), float(task_args[1])) if task == 'grid' else \
      setup_pendulum(dy_model, 2**14, 100) if task == 'pendulum' else \
      setup_blackjack(dy_model, 2**14, 100) if task == 'blackjack' else \
      setup_hex(dy_model, 2**14, 100, int(task_args[0])) if task == 'hex' else \
      setup_mountaincar(dy_model, 2**14, 1) if task == 'mountaincar' else \
      setup_cartpole(dy_model, 2**10, 1) if task == 'cartpole' else \
      setup_pocman(dy_model, 2**14, 100, task_args[0], task_args[1]) if task == 'pocman' else \
      None

    if initial_embeddings is not None and word_vocab is not None:
        initial_embeddings = nlp_data.read_embeddings(initial_embeddings, word_vocab)

    seqfeats_args = seqfeats.split('::')
    seqfeats = seqfeats_args[0]
    seqfeats_args = seqfeats_args[1:]

    def feature_builder(dy_model, n_types, output_id='', **kwargs):
        if seqfeats == 'bow':
            output_field = kwargs.get('output_field', 'tokens_feats') + output_id
            if 'output_field' in kwargs: del kwargs['output_field']
            return BOWFeatures(dy_model, n_types, output_field=output_field, **kwargs)
        elif seqfeats == 'grid':
            return LocalGridFeatures(train[0].width, train[0].height)
        elif seqfeats == 'pendulum':
            return PendulumFeatures()
        elif seqfeats == 'blackjack':
            return BlackjackFeatures()
        elif seqfeats == 'hex':
            return HexFeatures(int(task_args[0]))
        elif seqfeats == 'mountaincar':
            return MountainCarFeatures()
        elif seqfeats == 'cartpole':
            return CartPoleFeatures()
        elif seqfeats == 'pocman':
            return LocalPOCFeatures()
        elif seqfeats == 'rnn':
            output_field = kwargs.get('output_field', 'tokens_feats') + output_id
            if 'output_field' in kwargs: del kwargs['output_field']
            
            d_rnn = 50 if len(seqfeats_args) == 0 else int(seqfeats_args[0])
            n_layers = 1 if len(seqfeats_args) < 2 else int(seqfeats_args[1])

            init_embeds = None
            if kwargs.get('use_word_embeddings', False):
                init_embeds = initial_embeddings
                del kwargs['use_word_embeddings']
            return RNNFeatures(dy_model, n_types, rnn_type='LSTM',
                               d_emb=None if init_embeds is not None else 50,
                               output_field=output_field,
                               d_rnn=d_rnn,
                               n_layers=n_layers,
                               initial_embeddings=init_embeds,
                               learn_embeddings=init_embeds is None,
                               **kwargs)
        elif seqfeats == 'cnn':
            output_field = kwargs.get('output_field', 'tokens_feats') + output_id
            if 'output_field' in kwargs: del kwargs['output_field']
            
            init_embeds = None
            if kwargs.get('use_word_embeddings', False):
                init_embeds = initial_embeddings
                del kwargs['use_word_embeddings']
            return DilatedCNNFeatures(dy_model, n_types,
                                      d_emb=None if init_embeds is not None else 50,
                                      output_field=output_field,
                                      n_layers=8,
                                      passthrough=True,
                                      initial_embeddings=init_embeds,
                                      learn_embeddings=init_embeds is None,
                                      **kwargs)

    #transition_builder = TransitionBOW if seqfeats == 'bow' else TransitionRNN
    def transition_builder(dy_model, features, attention, n_labels, offset_id=''):
        return TransitionRNN(dy_model, features, attention, n_labels, h_name='h' + offset_id)

    p_layers=1
    hidden_dim=50
    for x in additional_args:
        if x.startswith('p_layers='): p_layers = int(x[9:])
        if x.startswith('p_dim='): hidden_dim = int(x[6:])

    bag_size = 5
    bootstrap = False
    extra_args = learning_method.split('::') + additional_args
    sweep_id = None
    for x in extra_args:
        if x.startswith('bag_size='): bag_size = int(x[9:])
        if x == 'bootstrap': bootstrap = True
        if x.startswith('sweep_id='): sweep_id = int(x[9:])
        
    if not bootstrap:
        features = mk_feats(feature_builder, '')
        transition = transition_builder(dy_model, features, attention(features), n_labels, '')
        policy = LinearPolicy(dy_model, transition, n_labels, loss_fn='huber', n_layers=p_layers, hidden_dim=50)
        if active:
            policy = CSActive(policy)
    else:
        greedy_predict = 'greedy_predict' in extra_args
        greedy_update = 'greedy_update' in extra_args
        
        all_transitions = []
        for i in range(bag_size):
            #offset_id = '%d' % i     # use this if you want each policy's feature set to be totally independent (uses lots of memory)
            offset_id = '' # use this to keep underlying (embedding+lstm) features shared
            features = mk_feats(feature_builder, offset_id)
            transition = transition_builder(dy_model, features, attention(features), n_labels, '%d' % i)
            all_transitions.append(transition)
        policy = BootstrapPolicy(dy_model, all_transitions, n_labels,
                                 loss_fn='huber',
                                 greedy_predict=greedy_predict,
                                 greedy_update=greedy_update,
                                 n_layers=p_layers,
                                 hidden_dim=hidden_dim)

    if load_initial_model_from is not None:
        # must do this before setup because aac makes additional params
        #dy_model.save('tmp_sweep_' + str(sweep_id))
        #nn = 1
        #for l in open('tmp_sweep_' + str(sweep_id)):
        #    if l.startswith('#'): 
        #        print >>sys.stderr, nn, l.strip()
        #        nn = nn + 1
        print 'loading model from %s' % load_initial_model_from
        dy_model.populate(load_initial_model_from)
        
    mk_learner, run_per_batch = \
      setup_banditlols(dy_model, learning_method) if learning_method.startswith('blols') else \
      setup_reinforce(dy_model, learning_method) if learning_method.startswith('reinforce') else \
      setup_aac(dy_model, learning_method, policy.features.dim) if learning_method.startswith('aac') else \
      setup_dagger(dy_model, learning_method) if learning_method.startswith('dagger') else \
      setup_aggrevate(dy_model, learning_method) if learning_method.startswith('aggrevate') else \
      setup_ppo(dy_model, learning_method) if learning_method.startswith('ppo') else \
      (None, [])

    Learner = lambda: mk_learner(reference, policy)

    optimizer = \
      dy.AdadeltaTrainer(dy_model) if opt_method == 'adadelta' else \
      dy.AdamTrainer(dy_model, alpha=learning_rate) if opt_method == 'adam' else \
      dy.AdagradTrainer(dy_model, learning_rate=learning_rate) if opt_method == 'adagrad' else \
      dy.MomentumSGDTrainer(dy_model, learning_rate=learning_rate) if opt_method == 'sgdmom' else \
      dy.RMSPropTrainer(dy_model, learning_rate=learning_rate) if opt_method == 'rmsprop' else \
      dy.SimpleSGDTrainer(dy_model, learning_rate=learning_rate) if opt_method == 'sgd' else \
      None

    if hasattr(policy, 'set_optimizer'):
        policy.set_optimizer(optimizer)

    def printit():
        #print optimizer.status()
        #if random.random() < 0.01:
        #    from arsenal import ip; ip()
        pass

    if mk_learner is None: # just evaluate
        print 'train loss:', macarico.util.evaluate(train, policy, losses[0])
        print 'dev loss:', macarico.util.evaluate(dev, policy, losses[0])
        return None

    maxlength=30
    #train = split_sequences(train, maxlength)
    print 'maxlength=%d' % maxlength
    
    history, _ = macarico.util.trainloop(
#        training_data      = train[:64],
        training_data = [x for x in train if not hasattr(x, 'tokens') or len(x.tokens)<=maxlength],
        dev_data           = dev, #[0:20:],
        policy             = policy,
        Learner            = Learner,
        losses             = losses,
        optimizer          = optimizer,
        run_per_batch      = run_per_batch + [printit],
        train_eval_skip    = None,
        bandit_evaluation  = not supervised,
        n_epochs           = 20 if supervised else 1,
        dy_model           = dy_model,
        save_best_model_to = save_best_model_to,
#        regularizer        = lambda w: 0.01 * dy.squared_norm(w)
        print_dots = False,
        print_freq = 2.0,
    )

    return history



if __name__ == '__main__' and len(sys.argv) == 2 and sys.argv[1] != '--sweep':
    learning_method = sys.argv[1]
    # print out some options
    opts = \
      setup_banditlols(None, learning_method) if learning_method.startswith('blols') else \
      setup_reinforce(None, learning_method) if learning_method.startswith('reinforce') else \
      setup_aac(None, learning_method) if learning_method.startswith('aac') else \
      setup_dagger(None, learning_method) if learning_method.startswith('dagger') else \
      setup_aggrevate(None, learning_method) if learning_method.startswith('aggrevate') else \
      None

    for opt in itertools.product(*opts):
        opt = [learning_method] + [x for x in opt if x != '']
        print '::'.join(opt)
    sys.exit(0)


#def read_output_file(fname):
#    try:
#        for l in open(fname, 'r'):
#            if not l.startswith('[(['): continue
#            l = np.array(eval(l.strip()))[:,:,0]
#    except IOError:
#        return None
    
if __name__ == '__main__' and len(sys.argv) >= 4 and sys.argv[1] != '--sweep':
    print sys.argv

    reps = 1
    initial_embeddings = None
    save_file, load_file = None, None
    token_vocab_file, pos_vocab_file = None, None
    seqfeats = 'rnn'
    #greedy_predict = True
    #greedy_update = True
    for x in sys.argv:
        if x.startswith('reps='): reps = int(x[5:])
        if x.startswith('embed='): initial_embeddings = x[6:]
        if x.startswith('save='): save_file = x[5:]
        if x.startswith('load='): load_file = x[5:]
        if x.startswith('tvoc='): token_vocab_file = x[5:]
        if x.startswith('pvoc='): pos_vocab_file = x[5:]
        if x.startswith('f='): seqfeats = x[2:]
        #if x.startswith('greedy_predict='): greedy_predict = (x[15:] == '1')
        #if x.startswith('greedy_update='): greedy_update = (x[14:] == '1')

    for rep in xrange(reps):
        this_save_file = save_file
        if reps > 1 and save_file is not None:
            this_save_file = save_file + '.%d' % rep
        res = run(sys.argv[1],  # task
                  sys.argv[2],  # learning_method
                  sys.argv[3],  # opt_method
                  float(sys.argv[4]),  # learning_rate
                  seqfeats,
                  'active' in sys.argv,
                  'supervised' in sys.argv,
                  initial_embeddings,
                  this_save_file, load_file,
                  token_vocab_file, pos_vocab_file,
                  sys.argv)
        print res
        print

    sys.exit(0)

sweep_complete = \
    set([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17,
         18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33,
         34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49,
         50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65,
         66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81,
         82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97,
         98, 99, 104, 105, 106, 107, 108, 109, 110, 111, 116, 117, 118,
         119, 120, 121, 122, 123, 128, 129, 130, 131, 132, 133, 134,
         135, 139, 140, 141, 142, 143, 144, 145, 146, 147, 152, 153,
         154, 155, 156, 157, 158, 159, 164, 165, 166, 167, 168, 169,
         170, 171, 176, 177, 178, 179, 180, 181, 182, 183, 188, 189,
         190, 191, 192, 193, 194, 195, 200, 201, 202, 203, 204, 205,
         206, 207, 212, 213, 214, 215, 216, 217, 218, 219, 224, 225,
         226, 227, 228, 229, 230, 231, 236, 237, 238, 239, 240, 241,
         242, 243, 248, 249, 250, 251, 252, 260, 261, 262, 263, 264,
         265, 267, 272, 273, 274, 275, 278, 279, 284, 285, 286, 287,
         288, 289, 290, 291, 296, 297, 298, 299, 300, 301, 302, 303,
         308, 309, 310, 311, 312, 313, 314, 315, 320, 321, 322, 323,
         324, 325, 326, 327, 332, 333, 334, 335, 336, 337, 338, 339,
         344, 345, 346, 347, 348, 349, 350, 351, 356, 357, 358, 359,
         360, 361, 362, 363, 368, 369, 370, 371, 372, 373, 374, 375,
         380, 381, 382, 383, 384, 385, 386, 387, 392, 393, 394, 395,
         396, 397, 398, 399, 404, 405, 406, 407, 408, 409, 410, 411,
         416, 417, 418, 419, 420, 421, 422, 423, 428, 429, 430, 431,
         432, 434, 440, 441, 442, 443, 444, 452, 453, 454, 455, 457,
         458, 459, 464, 465, 466, 467, 469, 470, 476, 477, 478, 479,
         480, 481, 482, 483, 488, 489, 490, 491, 492, 493, 494, 495,
         500, 501, 502, 503, 504, 505, 506, 507, 512, 513, 514, 515,
         516, 517, 518, 519, 523, 524, 525, 526, 527, 528, 529, 530,
         531, 536, 537, 538, 539, 540, 541, 542, 543, 548, 549, 550,
         551, 552, 553, 554, 555, 560, 561, 562, 563, 564, 565, 566,
         567, 572, 573, 574, 575, 576, 578, 579, 584, 585, 586, 587,
         589, 590, 591, 596, 597, 598, 599, 600, 601, 602, 603, 608,
         609, 610, 611, 612, 613, 614, 615, 620, 621, 622, 623, 624,
         632, 633, 634, 635, 636, 637, 639, 644, 645, 646, 647, 650,
         651, 656, 657, 658, 659, 660, 661, 662, 663, 668, 669, 670,
         671, 672, 673, 674, 675, 680, 681, 682, 683, 684, 685, 686,
         687, 689, 692, 693, 694, 695, 696, 697, 698, 699, 704, 705,
         706, 707, 708, 709, 710, 711, 712, 715, 716, 717, 718, 719,
         720, 721, 722, 723, 728, 729, 730, 731, 732, 733, 734, 735,
         740, 741, 742, 743, 744, 745, 746, 747, 752, 753, 754, 755,
         756, 757, 758, 759, 764, 765, 766, 767, 768, 769, 770, 771,
         776, 777, 778, 779, 781, 782, 783, 788, 789, 790, 791, 792,
         793, 794, 795, 800, 801, 802, 803, 804, 805, 806, 807, 812,
         813, 814, 815, 816, 817, 818, 824, 825, 826, 827, 828, 829,
         831, 836, 837, 838, 839, 840, 841, 843, 848, 849, 850, 851,
         852, 854, 855, 860, 861, 862, 863, 864, 865, 866, 867, 872,
         873, 874, 875, 878, 879, 884, 885, 886, 887, 888, 889, 891,
         894, 896, 897, 898, 899, 900, 901, 902, 903, 905, 908, 909,
         910, 911, 912, 913, 914, 915, 920, 921, 922, 923, 924, 925,
         926, 927, 932, 933, 934, 935, 936, 937, 938, 939, 944, 945,
         946, 947, 948, 949, 950, 951, 956, 957, 958, 959, 960, 961,
         962, 963, 968, 969, 970, 971, 980, 981, 983, 984, 985, 986,
         992, 993, 994, 995, 996, 997, 998, 999, 1004, 1005, 1006, 1007,
         1009, 1010, 1011, 1016, 1017, 1018, 1019, 1020, 1022, 1023,
         1028, 1029, 1030, 1031, 1032, 1033, 1040, 1041, 1042, 1043,
         1045, 1052, 1053, 1054, 1055, 1056, 1057, 1058, 1059, 1064,
         1065, 1066, 1067, 1070, 1071, 1076, 1077, 1078, 1079, 1080,
         1081, 1083, 1088, 1089, 1090, 1091, 1092, 1093, 1094, 1095,
         1100, 1101, 1102, 1103, 1104, 1105, 1106, 1107, 1112, 1113,
         1114, 1115, 1116, 1117, 1118, 1119, 1124, 1125, 1126, 1127,
         1128, 1129, 1130, 1131, 1136, 1137, 1138, 1139, 1140, 1141,
         1142, 1143, 1148, 1149, 1150, 1151, 1152, 1153, 1154, 1155,
         1160, 1161, 1162, 1163, 1172, 1173, 1175, 1176, 1177, 1178,
         1184, 1185, 1186, 1187, 1188, 1189, 1190, 1191, 1196, 1197,
         1198, 1199, 1201, 1203, 1208, 1209, 1210, 1211, 1220, 1221,
         1222, 1223, 1232, 1233, 1234, 1235, 1236, 1237, 1244, 1245,
         1246, 1247, 1248, 1249, 1250, 1256, 1257, 1258, 1259, 1260,
         1261, 1262, 1263, 1268, 1269, 1270, 1271, 1272, 1273, 1274,
         1275, 1280, 1281, 1282, 1283, 1284, 1285, 1286, 1287, 1292,
         1293, 1294, 1295, 1296, 1297, 1298, 1299, 1304, 1305, 1306,
         1307, 1308, 1309, 1310, 1316, 1317, 1318, 1319, 1320, 1321,
         1322, 1328, 1329, 1330, 1331, 1332, 1333, 1334, 1340, 1341,
         1342, 1343, 1344, 1345, 1347, 1352, 1353, 1354, 1355, 1356,
         1358, 1359, 1364, 1365, 1366, 1367, 1368, 1369, 1370, 1376,
         1377, 1378, 1379, 1381, 1388, 1389, 1390, 1391, 1400, 1401,
         1402, 1403, 1412, 1413, 1414, 1415, 1424, 1425, 1426, 1427,
         1436, 1437, 1438, 1439, 1440, 1441, 1442, 1448, 1449, 1450,
         1451, 1452, 1453, 1454, 1455, 1460, 1461, 1462, 1463, 1464,
         1465, 1466, 1467, 1472, 1473, 1474, 1475, 1476, 1477, 1478,
         1479, 1484, 1485, 1486, 1487, 1490, 1496, 1497, 1498, 1499,
         1508, 1509, 1510, 1511, 1520, 1521, 1522, 1523, 1532, 1533,
         1534, 1535, 1544, 1545, 1546, 1547, 1556, 1557, 1558, 1559,
         1568, 1569, 1570, 1571, 1580, 1581, 1582, 1583, 1592, 1593,
         1594, 1595, 1604, 1605, 1606, 1607, 1616, 1617, 1618, 1619,
         1628, 1629, 1630, 1631, 1632, 1633, 1635, 1640, 1641, 1642,
         1643, 1647, 1652, 1653, 1654, 1655, 1657, 1658, 1664, 1665,
         1666, 1667, 1670, 1676, 1677, 1678, 1679, 1688, 1689, 1690,
         1691, 1700, 1701, 1702, 1703, 1712, 1713, 1714, 1715, 1724,
         1725, 1726, 1727, 1736, 1737, 1738, 1739, 1748, 1749, 1750,
         1751])


def my_permutation(A, seed=90210):
    _MULT, _ADD = 3819047, 94281731
    n = len(A)
    D = { n: v for n, v in enumerate(A) }
    B = []
    i = seed
    while len(B) < n:
        i = (i * _MULT + _ADD) % n
        while i not in D:
            i = (i + 1) % n
        B.append(D[i])
        del D[i]
    return B

if __name__ == '__main__' and len(sys.argv) >= 2 and sys.argv[1] == '--sweep':
    algs = []

    # dagger: supervised upper bound
    for p_rin in [0.0, 0.999, 0.99999]:
        algs += ['dagger::p_rin=%g' % p_rin]
    # reinforce
    algs += ['reinforce',
             'reinforce::baseline=0.8']
    #for baseline in [0.2, 0.5, 0.8]:
    #    algs += ['reinforce::baseline=%g' % baseline]
#                 'reinforce::baseline=%g::maxd=1']
    # a2c
    for mult in [0.5, 1.0, 2.0]:
        algs += ['aac::mult=%g' % mult]
    # blols
    for update in ['ips', 'dr', 'mtr']:
        #for multidev in ['', '::multidev']:
        for multidev in ['::multidev']:
            for upc in ['', '::upc']:
                for oft in ['', '::oft']:
                    for exp in [0.5, 1.0]:
                        explore = '::explore=%g' % exp
                        # uniform exploration
                        algs += ['blols::' + update + '::uniform' + multidev + upc + oft + explore]
                        # boltzmann exploration
                        algs += ['blols::' + update + '::boltzmann::temp=%g' % temp + multidev + upc + oft + explore \
                                 for temp in [0.2, 1.0, 5.0]]
                        # bootstrap exploration
                        #for bag_size in [5]:
                        s = 'blols::' + update + '::bootstrap' + multidev + upc + oft + explore
                            #s = 'blols::' + update + '::bootstrap::bag_size=%d' % bag_size + multidev + upc + oft + explore
                            #s = 'blols::' + update + '::uniform' + multidev + upc + oft + explore
                        algs += [s,
                                 s + '::greedy_update',
                                 s + '::greedy_predict',
                                 s + '::greedy_update::greedy_predict']
                            
    tasks = ['pos-wsj', 'dep-wsj', 'ctb-sc']
    opts = ['adam']
    lrs = [0.0001, 0.0005, 0.001, 0.005]

    all_settings = list(itertools.product(algs, tasks, opts, lrs))
    all_settings += [('noop', task, 'adam', 0) for task in tasks]
    all_settings += [('noop::bootstrap', task, 'adam', 0) for task in tasks]

    # need to add really small learning rates for things other than blols
    lrs = [1e-6, 1e-8]
    all_settings += list(itertools.product([a for a in algs if 'blols' not in a], tasks, opts, lrs))

    # need to add ppo
    lrs = [0.0001, 0.0005, 0.001, 0.005, 1e-6, 1e-8]
    algs = []
    for epsilon in [0.01, 0.05, 0.1, 0.2, 0.4, 0.8]:
        algs += ['ppo::epsilon=%g::baseline=0.0' % epsilon,
                 'ppo::epsilon=%g::baseline=0.8' % epsilon]
    all_settings += list(itertools.product(algs, tasks, opts, lrs))

    all_settings_arg_to_id = my_permutation(range(len(all_settings)))
    #all_settings_arg_to_id = range(len(all_settings))

    if len(sys.argv) == 2:
        # get options
        print len(all_settings)
        sys.exit(0)

    if sys.argv[2] == 'list':
        for n in xrange(len(all_settings)):
            print n, all_settings[all_settings_arg_to_id[n]], '*' if all_settings_arg_to_id[n] in sweep_complete else ''
        sys.exit(0)

#    if sys.argv[2] == 'results':
#        for n in xrange(all_settings):
#            res = read_output_file('output/blols_%d.out' % n)
#            if res is None: continue
#            print '%g\t%s\t%s' % (res[0], ' '.join(all_settings[sweep_id]), res)
#        sys.exit(0)
        
    # otherwise, run
    sweep_id = int(sys.argv[2])
    if sweep_id < 0 or sweep_id >= len(all_settings):
        print 'invalid sweep_id %d (must be in [0, %d))' % (sweep_id, len(all_settings))
        sys.exit(-1)

    sweep_id0 = sweep_id
    sweep_id = all_settings_arg_to_id[sweep_id]

    if sweep_id in sweep_complete:
        print 'already done with %d (aka %d)' % (sweep_id0, sweep_id)
        if len(sys.argv) > 3 and (sys.argv[3] == '--force' or sys.argv[3] == '-f'):
            print 'running anyway because --force'
        else:
            sys.exit(0)

    print 'running sweep %d == %d' % (sweep_id0, sweep_id)
            
    alg, task, opt, lr = all_settings[sweep_id]
    #lr /= 10
    
    bag_size = None
    if 'bootstrap' in alg:
        if   task == 'pos-wsj': embed, d_rnn, n_layers, p_layers, load, bag_size = 300, 300, 1, 2, DATA_DIR + 'data/adam_0.001_dagger_0.99999_pos-tweet_300_300_1_2_bootstrap_10_7.model', 10
        #if   task == 'pos-wsj': embed, d_rnn, n_layers, p_layers, load, bag_size = 300, 300, 1, 2, DATA_DIR + 'data/adam_0.001_dagger_0.99999_pos-tweet_300_300_1_2_bootstrap_3_4.model', 3
        #if   task == 'pos-wsj': embed, d_rnn, n_layers, p_layers, load, bag_size = 100, 100, 1, 2, DATA_DIR + 'data/adam_0.001_dagger_0.999_pos-tweet_100_100_1_2_bootstrap_3_4.model', 3
        elif task == 'dep-wsj': embed, d_rnn, n_layers, p_layers, load, bag_size = 300, 300, 1, 2, DATA_DIR + 'data/adam_0.001_dagger_0.99999_dep-tweet_300_300_1_2_bootstrap_5_0.model', 5
        elif task == 'ctb-sc':  embed, d_rnn, n_layers, p_layers, load, bag_size = 300,  50, 2, 1, DATA_DIR + 'data/adam_0.0005_dagger_0.999_ctb-nw_300_50_2_1_bootstrap_3_4.model', 3
        else: raise Exception('unknown task %s' % task)
    else:
        if   task == 'pos-wsj': embed, d_rnn, n_layers, p_layers, load = 300, 300, 1, 2, DATA_DIR + 'data/adam_0.001_dagger_0.99999_pos-tweet_300_300_1_2_0.model'
        elif task == 'dep-wsj': embed, d_rnn, n_layers, p_layers, load = 300, 300, 1, 2, DATA_DIR + 'data/adam_0.001_dagger_0.99999_dep-tweet_300_300_1_2_8.model'
        elif task == 'ctb-sc':  embed, d_rnn, n_layers, p_layers, load = 300,  50, 2, 1, DATA_DIR + 'data/adam_0.0005_dagger_0.999_ctb-nw_300_50_2_1_7.model'
        else: raise Exception('unknown task %s' % task)

    print alg, task, opt, lr
    print embed, d_rnn, n_layers, p_layers, load

    addl_args = ['p_layers=%d' % p_layers]
    if bag_size is not None:
        addl_args += ['bootstrap', 'bag_size=%d' % bag_size]

    n_rep = 1 if 'noop' in alg else 3
    n_rep = 1
        
    for rep in xrange(n_rep):
        res = run(task, alg, opt, lr,
                  'rnn::%d::%d' % (d_rnn, n_layers),
                  False, #active
                  False, #supervised
                  str(embed),
                  None,
                  load,
                  None,
                  None,
                  addl_args + ['sweep_id='+ str(sweep_id)]
                  )
        print res
                  
                  
        
    sys.exit(0)

"""
models: 
adam_0.001_dagger_0.99999_pos-tweet_300_300_1_2_0.model adam_0.001_dagger_0.99999_dep-tweet_300_300_1_2_8.model adam_0.0005_dagger_0.999_ctb-nw_300_50_2_1_7.model adam_0.001_dagger_0.99999_pos-tweet_300_300_1_2_bootstrap_10_7.model adam_0.001_dagger_0.99999_dep-tweet_300_300_1_2_bootstrap_5_0.model adam_0.0005_dagger_0.999_ctb-nw_300_50_2_1_bootstrap_3_4.model

supervised pretraining results

no bootstrap

2.1162790697674421      adam_0.001_dagger_0.99999_pos-tweet_300_300_1_2_0  0
2.434108527131783       adam_0.001_dagger_0.99999_dep-tweet_300_300_1_2_8  0
2.3975757575757575      adam_0.0005_dagger_0.999_ctb-nw_300_50_2_1_7       0


bootstrap

2.13953488372093        adam_0.001_dagger_0.99999_pos-tweet_300_300_1_2_bootstrap_10_7     0
2.4186046511627906      adam_0.001_dagger_0.99999_dep-tweet_300_300_1_2_bootstrap_5_0      0
2.5533333333333332      adam_0.0005_dagger_0.999_ctb-nw_300_50_2_1_bootstrap_3_4   0


smaller bootstrap for pos-tweet
2.186046511627907       adam_0.001_dagger_0.99999_pos-tweet_300_300_1_2_bootstrap_3_4      0

even smaller
2.2635658914728682      adam_0.001_dagger_0.999_pos-tweet_100_100_1_2_bootstrap_3_4        0


"""

    
# if __name__ == '__main__' and len(sys.argv) > 2:
#     print sys.argv

#     learning_method = int(sys.argv[1])
#     exploration = int(sys.argv[2])
#     temperature = float(sys.argv[3])
#     baseline = float(sys.argv[4])
#     uniform = sys.argv[5] == 'True'
#     max_deviations = None if sys.argv[6] == 'None' else int(sys.argv[6])
#     use_prefix_costs = sys.argv[7] == 'True'
#     offset_t = sys.argv[8] == 'True'
#     method = sys.argv[9]
#     learning_rate = float(sys.argv[10])
#     loss_fn = sys.argv[11]
#     random_seed = int(sys.argv[12])
#     num_rep = int(sys.argv[13])

#     for _ in xrange(num_rep):
#         test1(learning_method=learning_method,
#               exploration=exploration,
#               N=65536,
#               n_types=160,
#               n_labels=4,
#               length=20,
#               random_seed=random_seed,
#               bow=False,
#               method=method,
#               temperature=temperature,
#               p_ref=0,
#               baseline=baseline,
#               uniform=uniform,
#               max_deviations=max_deviations,
#               use_prefix_costs=use_prefix_costs,
#               offset_t=offset_t,
#               learning_rate=learning_rate,
#               loss_fn=loss_fn,
#         )
#     #bow = 'bow' in sys.argv
#     #method = 'banditlols'
#     #if 'reinforce' in sys.argv: method = 'reinforce'
#     #if 'aac' in sys.argv: method = 'aac'
#     #test1(learning_method=int(sys.argv[1]),
#     #      exploration=int(sys.argv[2]),
#     #      N=int(sys.argv[3]),
#     #      n_types=int(sys.argv[4]),
#     #      n_labels=int(sys.argv[5]),
#     #      length=int(sys.argv[6]),
#     #      random_seed=int(sys.argv[7]),
#     #      bow=bow,
#     #      method=method,
#     #      temperature=float(sys.argv[8]),
#     #      p_ref=float(sys.argv[9]),
#     #)
#     sys.exit()


# for name, res in pickle.load(open('big_banditlols.new.saved')): globals()[name] = res

# if False:
#     blols_1 = test1(1, 0, N=65536, n_types=160, n_labels=4, length=20, random_seed=20001, bow=True, method='banditlols', temperature=1, p_ref=1, baseline=0.0, uniform=False, max_deviations=None)
#     blols_2 = test1(2, 0, N=65536, n_types=160, n_labels=4, length=20, random_seed=20001, bow=True, method='banditlols', temperature=1, p_ref=1, baseline=0.0, uniform=False, max_deviations=None)
#     blols_3 = test1(3, 0, N=65536, n_types=160, n_labels=4, length=20, random_seed=20001, bow=True, method='banditlols', temperature=1, p_ref=1, baseline=0.0, uniform=False, max_deviations=None)
#     blols_4 = test1(4, 0, N=65536, n_types=160, n_labels=4, length=20, random_seed=20001, bow=True, method='banditlols', temperature=1, p_ref=1, baseline=0.0, uniform=False, max_deviations=None)

#     blols_1_bl = test1(1, 0, N=65536, n_types=160, n_labels=4, length=20, random_seed=20001, bow=True, method='banditlols', temperature=1, p_ref=1, baseline=0.8, uniform=False, max_deviations=None)
#     blols_2_bl = test1(2, 0, N=65536, n_types=160, n_labels=4, length=20, random_seed=20001, bow=True, method='banditlols', temperature=1, p_ref=1, baseline=0.8, uniform=False, max_deviations=None)
#     blols_3_bl = test1(3, 0, N=65536, n_types=160, n_labels=4, length=20, random_seed=20001, bow=True, method='banditlols', temperature=1, p_ref=1, baseline=0.8, uniform=False, max_deviations=None)
#     blols_4_bl = test1(4, 0, N=65536, n_types=160, n_labels=4, length=20, random_seed=20001, bow=True, method='banditlols', temperature=1, p_ref=1, baseline=0.8, uniform=False, max_deviations=None)

# test1(2, 1, N=65536, n_types=160, n_labels=4, length=20, random_seed=20001, bow=True, method='banditlols', temperature=0.2, p_ref=0, baseline=0, uniform=False, max_deviations=None, use_prefix_costs=True, offset_t=True)


# if False:
#     reinforce = test1(0, 0, N=65536, n_types=160, n_labels=4, length=20, random_seed=20001, bow=True, method='reinforce', temperature=1, p_ref=1, baseline=0.8, uniform=False, max_deviations=None)
#     reinforce_nobl = test1(0, 0, N=65536, n_types=160, n_labels=4, length=20, random_seed=20001, bow=True, method='reinforce', temperature=1, p_ref=1, baseline=0.0, uniform=False, max_deviations=None)
#     reinforce_md1 = test1(0, 0, N=65536, n_types=160, n_labels=4, length=20, random_seed=20001, bow=True, method='reinforce', temperature=1, p_ref=1, baseline=0.8, uniform=False, max_deviations=1)
#     reinforce_uni = test1(0, 0, N=65536, n_types=160, n_labels=4, length=20, random_seed=20001, bow=True, method='reinforce', temperature=1, p_ref=1, baseline=0.8, uniform=True, max_deviations=None)
#     reinforce_md1_uni = test1(0, 0, N=65536, n_types=160, n_labels=4, length=20, random_seed=20001, bow=True, method='reinforce', temperature=1, p_ref=1, baseline=0.8, uniform=True, max_deviations=1)
#     reinforce_md1_nobl = test1(0, 0, N=65536, n_types=160, n_labels=4, length=20, random_seed=20001, bow=True, method='reinforce', temperature=1, p_ref=1, baseline=0.0, uniform=False, max_deviations=1)
#     reinforce_uni_nobl = test1(0, 0, N=65536, n_types=160, n_labels=4, length=20, random_seed=20001, bow=True, method='reinforce', temperature=1, p_ref=1, baseline=0.0, uniform=True, max_deviations=None)
#     reinforce_md1_uni_nobl = test1(0, 0, N=65536, n_types=160, n_labels=4, length=20, random_seed=20001, bow=True, method='reinforce', temperature=1, p_ref=1, baseline=0, uniform=True, max_deviations=1)
#     dumpit()

def print_one(name, name2, X=None):
    if X is None:
        X = globals()[name]

    if isinstance(X, list):
        X = np.array(X).reshape(len(X),2)

    tail_score  = X[-1,0]
    tail_score2 = (2 ** (np.arange(17) + 1) * X[:,1] / sum(2 ** (np.arange(17) + 1))).sum()
    tail_score3 = X[-1,1]
    tail_score  = str(int(100 * tail_score ) / 100)
    tail_score2 = str(int(100 * tail_score2) / 100)
    tail_score3 = str(int(100 * tail_score3) / 100)

    name = name.replace('_1', '_ips').replace('_2', '_dir').replace('_3', '_mtr').replace('_4', '_mtA')

    print name, ' ' * (22-len(name)), \
        name2, ' ' * (22-len(name2)), \
        tail_score , ' ' * (5 - len(tail_score)), \
        tail_score2, ' ' * (5 - len(tail_score2)), \
        tail_score3, ' ' * (5 - len(tail_score3)), \
        ''

def read_bbl_out(fname):
    D = []
    me = None
    args = None
    for l in open(fname, 'r'):
        if 'dynet' in l: continue
        if not l.startswith('['): continue
        l = eval(l.strip())
        if l[0] == 'big_banditlols.py':
            learning_method = int(l[1])
            exploration = int(l[2])
            temperature = float(l[3])
            baseline = float(l[4])
            uniform = l[5] == 'True'
            max_deviations = None if l[6] == 'None' else int(l[6])
            use_prefix_costs = l[7] == 'True'
            offset_t = l[8] == 'True'
            method = l[9]
            learning_rate = float(l[10])
            loss_fn = l[11]
            random_seed = int(l[12])
            num_rep = int(l[13])

            learning_method = 'ips' if learning_method == 0 else 'dir' if learning_method == 1 else 'mtr' if learning_method == 2 else 'mtA'

            method = 'rnfrc' if method == 'reinforce' else 'blols'
            exploration = 'uni' if exploration == 0 else 'btz' if exploration == 1 else 'bzb'
            temperature = 'temp:' + str(temperature)
            baseline = 'bl' if baseline > 0 else ''
            uniform = 'uni' if uniform else ''
            max_deviations = 'md1' if max_deviations is not None else ''
            use_prefix_costs = 'pre' if use_prefix_costs else ''
            offset_t = 'oft' if offset_t else ''
            loss_fn = 'hub' if loss_fn == 'huber' else 'sqr'
            learning_rate = 'lr:' + str(learning_rate)

            if method == 'blols':
                baseline = ''
                uniform = ''
                max_deviations = ''
            else:
                learning_method = ''
                exploration = ''
                use_prefix_costs = ''
                offset_t = ''
                loss_fn = ''

            me = ' '.join(map(str, [x for x in [method, learning_method, exploration, baseline, uniform, max_deviations, use_prefix_costs, offset_t] if x != '']))
            args = ' '.join(map(str, [temperature, learning_rate, loss_fn]))
        else:
            l = np.array(l)[:,:,0]
            D.append(l)
    #print_one('me', x/n)
    return me, args, D



d = {}
for fname in glob.glob('bbl_out/*'):
    me, args, D = read_bbl_out(fname)
    l = len(D)
    if l == 0: continue
    D = np.array(D)
    X = D.mean(axis=0)
    V = D.std(axis=0)
    score = (2 ** (np.arange(17) + 1) * X[:,1] / sum(2 ** (np.arange(17) + 1))).sum()
    if me not in d or score < d[me][0]:
        d[me] = (score, args, X, V / np.sqrt(l))

    #d[me][args]
#read_bbl_out('bbl_out/lols.1068')


count = {}
names0 = []
col = 1

from matplotlib.pyplot import *

fig, ax = subplots(1)

V_all = []
colors = 'brg'
print
d = sorted([(score, me, args, X, V) for me, (score, args, X, V) in d.iteritems()])
for (score, name, args, X, V) in d:
    print_one(name, args, X)

    if name not in ['rnfrc bl uni', 'rnfrc bl', 'blols mtA btz oft', 'rnfrc bl md1', 'rnfrc bl uni md1']:
        continue

#for name in names:
    #if 'blols' in name and '_learn' not in name: continue
    #data = globals()[name]
    #X = np.array(data).reshape(len(data),2)

    line_style = ''

    if 'rnfrc' in name: line_style = ':'
    elif 'pre' in name and 'oft' in name: line_style = '-'
    elif 'pre' in name: line_style = '--'
    else: line_style = '-.'

    #if 'blols'     in name and '_bl' not in name: line_style = '-'
    #if 'blols'     in name and '_bl'     in name: line_style = ':'
    #if 'blols' not in name and '_nobl'   in name: line_style = '--'
    #if 'blols' not in name and '_nobl' not in name: line_style = '-.'
    #if   'blols' in name and '_pref' in name and '_os' in name: line_style = '-'
    #elif 'blols' in name and '_pref' in name: line_style = '--'
    #elif 'blols' in name: line_style = ':'
    #else: line_style = '-.'

    #if 'reinforce' in name: continue
    #if 'blols' in name: continue

    color_id = count.get(line_style, 0)
    if color_id < len(colors):
        color = colors[color_id]
        count[line_style] = 1 + count.get(line_style, 0)

        T = np.arange(17)+1
        ax.plot(T, X[:,col], color + line_style, linewidth=4)
        ax.fill_between(T, X[:,col]+V[:,col], X[:,col]-V[:,col], facecolor=color, alpha=0.2)
        names0.append(name)

    V_all += list(V.flatten())
    #print_one(name, X)

#print sum(V_all) / len(V_all)


legend(names0, fontsize='xx-large')
show(True)



"""
python big_banditlols.py pos-tweet dagger::0.999 rmsprop 0.001 supervised embed=data/glove.6B.50d.txt.gz
2.85	0.785051663	50/50
2.63	0.8016441662	50/150
2.23	0.8318123539	100/150
2.37                    300/50
2.29	0.8272871257	300/150
2.37	0.8212534882	300/300
2.51	 	        50/50+bow
2.26	0.8295497398	100/150+bow

ctb-nw
3.04                    50/50
2.75                    300/50
                        300/50+layer_norm -- too slow
2.50                    300/50+2_layers
2.78                    300/150
                        300/300

"""

"""
% python big_banditlols.py --sweep 11949
[dynet] random seed: 1578253893
[dynet] allocating memory: 512MB
[dynet] memory allocation done.
blols::dr::bootstrap::bag_size=5::multidev::explore=0.2::greedy_update dep-wsj adam 0.01
300 300 1 2 ./data/adam_0.001_dagger_0.99999_dep-tweet_300_300_1_2_bootstrap_5_0.model

# {'save_best_model_to': None, 'task': 'dep-wsj', 'token_vocab_file': None, 'load_initial_model_from': './data/adam_0.001_dagger_0.99999_dep-tweet_300_300_1_2_bootstrap_5_0.model', 'learning_rate': 0.01, 'pos_vocab_file': None, 'opt_method': 'adam', 'supervised': False, 'active': False, 'seqfeats': 'rnn::300::1', 'additional_args': ['p_layers=2', 'bootstrap', 'bag_size=5'], 'learning_method': 'blols::dr::bootstrap::bag_size=5::multidev::explore=0.2::greedy_update', 'initial_embeddings': '300'}

read 11334 items from ./data/glove.6B.300d.txt.gz (out of 11638)
tr_lal      de_lal             N  epoch  rand_dev_truth          rand_dev_pred
102.693 | 10.000000   13.575501          1      1  [[8, 2, 8, 2, 7, 7,..]  [0->12 1->2 2->12 3..]  *
Traceback (most recent call last):
  File "big_banditlols.py", line 684, in <module>
    addl_args
  File "big_banditlols.py", line 536, in run
    print_dots = False,
  File "/home/hal/projects/macarico/macarico/util.py", line 194, in trainloop
    bl, sq = learning_alg(ex)
  File "/home/hal/projects/macarico/macarico/util.py", line 104, in learning_alg
    env.run_episode(learner)
  File "/home/hal/projects/macarico/macarico/tasks/dependency_parser.py", line 155, in run_episode
    assert self.a in self.actions, 'policy %s returned an invalid transition "%s" (must be one of %s)!' % (type(policy), self.a, self.actions)
AssertionError: policy <class 'macarico.lts.lols.BanditLOLSMultiDev'> returned an invalid transition "0" (must be one of set([1, 2]))!
"""
