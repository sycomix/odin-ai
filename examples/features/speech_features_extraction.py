# ===========================================================================
# Without PCA:
#   ncpu=1:  16s
#   ncpu=2:  9.82
#   ncpu=4:  5.9s
#   ncpu=8:  4.3
#   ncpu=12: 4.0
# ===========================================================================
from __future__ import print_function, division, absolute_import
import matplotlib
matplotlib.use('Agg')

import numpy as np
import shutil
import os
import sys
from odin import visual
from odin.utils import ctext
from odin import fuel as F, utils, preprocessing as pp
from collections import defaultdict
from odin.ml import MiniBatchPCA

# ===========================================================================
# set LOG path
# ===========================================================================
LOG_PATH = utils.get_logpath('speech_features_extraction.log',
                             override=True)
utils.stdio(LOG_PATH)
# ===========================================================================
# Const
# ===========================================================================
PCA = True
center = True
if True:
    audio = F.WDIGITS.get_dataset()
else:
    audio = F.DIGITS.get_dataset()
print(audio)
all_files = list(audio['indices'].keys())
print("Found %d (.wav) files" % len(all_files))
output_path = utils.get_datasetpath(name='digit')
# ===========================================================================
# Extractor
# ===========================================================================
padding = False
frame_length = 0.025
step_length = 0.005
dtype = 'float32'
extractors = pp.make_pipeline(steps=[
    pp.speech.AudioReader(sr_new=8000, best_resample=True,
                          remove_dc_n_dither=True, preemphasis=0.97,
                          dataset=audio),
    pp.speech.SpectraExtractor(frame_length=frame_length,
                               step_length=step_length,
                               nfft=512, nmels=40, nceps=20,
                               fmin=64, fmax=4000, padding=padding),
    # pp.speech.CQTExtractor(frame_length=frame_length,
    #                        step_length=step_length,
    #                        nbins=96, nmels=40, nceps=20,
    #                        fmin=64, fmax=4000, padding=padding),
    # pp.speech.PitchExtractor(frame_length=0.05, step_length=step_length,
    #                          threshold=0.5, f0=False, algo='swipe',
    #                          fmin=64, fmax=400),
    # pp.speech.openSMILEpitch(frame_length=0.06, step_length=step_length,
    #                          voiceProb=True, loudness=True),
    pp.speech.SADextractor(nb_mixture=3, nb_train_it=25,
                           feat_type='energy'),
    pp.speech.RASTAfilter(rasta=True, sdc=0),
    pp.base.DeltaExtractor(width=9, order=(0, 1, 2), axis=0,
                      feat_type=('mspec', 'qmspec', 'mfcc', 'qmfcc',
                                 'energy', 'pitch')),
    pp.speech.AcousticNorm(mean_var_norm=True, window_mean_var_norm=True,
                           feat_type=('mspec', 'mfcc',
                                      'qspec', 'qmfcc', 'qmspec')),
    pp.base.EqualizeShape0(feat_type=('spec', 'mspec', 'mfcc',
                                      'qspec', 'qmspec', 'qmfcc',
                                      'pitch', 'f0', 'sad', 'energy',
                                      'sap', 'loudness')),
    pp.base.RemoveFeatures(feat_type=('raw')),
    pp.base.RunningStatistics(),
    pp.base.AsType({'spec': dtype, 'mspec': dtype, 'mfcc': dtype,
                    'qspec': dtype, 'qmspec': dtype, 'qmfcc': dtype,
                    'pitch': dtype, 'f0': dtype, 'sap': dtype,
                    'sad': dtype, 'energy': dtype, 'loudness': dtype,
                    'raw': dtype}),
], debug=False)
# extractors.transform(all_files[0])
# exit()
# ===========================================================================
# Processor
# ===========================================================================
processor = pp.FeatureProcessor(all_files, extractors, output_path,
                                ncache=0.12, ncpu=None, override=True)
with utils.UnitTimer():
    processor.run()
readme_path = os.path.join(audio.path, [i for i in os.listdir(audio.path)
                                        if 'README' in i][0])
shutil.copy(readme_path,
            os.path.join(output_path, 'README.md'))
pp.calculate_pca(processor, override=True)
# ====== check the preprocessed dataset ====== #
print('Output path:', output_path)
ds = F.Dataset(output_path, read_only=True)
pp.validate_features(ds, path='/tmp/digits', nb_samples=8, override=True)
print(ds)
# ====== print pipeline ====== #
padding = '  '
print(ctext("* Pipeline:", 'red'))
for _, extractor in ds['pipeline'].steps:
    for line in str(extractor).split('\n'):
        print(padding, line)
# ====== print config ====== #
print(ctext("* Configurations:", 'red'))
for i, j in ds['config'].items():
    print(padding, i, ':', j)
# ====== check PCA components ====== #
for n in ds.keys():
    if '_pca' in n:
        pca = ds[n]
        if pca.components_ is None:
            print(ctext(n, 'yellow'), 'components is None !')
        elif np.any(np.isnan(pca.components_)):
            print(ctext(n, 'yellow'), 'contains NaN !')
        else:
            print(ctext(n, 'yellow'),
                ':', ' '.join(['%.2f' % i + '-' + '%.2f' % j
                for i, j in zip(pca.explained_variance_ratio_[:8],
                                pca.explained_variance_[:8])]))
exit() # TODO: fix here
# ====== plot the processed files ====== #
figpath = '/tmp/speech_features.pdf'
files = np.random.choice(list(ds['indices'].keys()),
                         size=8, replace=False)
for f in files:
    with visual.figure(ncol = 1, nrow = 5, dpi = 180,
                       show = False, tight_layout = True, title = f):
        start, end = ds['indices'][f]
        vad = ds['sad'][start:end]
        pitch = ds['pitch'][start:end].astype('float32')
        energy = ds['energy'][start:end][:].astype('float32')
        spec = ds['spec'][start:end].astype('float32')
        mspec = ds['mspec'][start:end][:, :40].astype('float32')
        mfcc = ds['mfcc'][start:end][:, :20].astype('float32')
        visual.subplot(5, 1, 1)
        visual.plot(energy.ravel())
        visual.subplot(5, 1, 2)
        visual.plot(pitch.ravel())
        visual.subplot(5, 1, 3)
        visual.plot_spectrogram(spec.T, vad = vad)
        visual.subplot(5, 1, 4)
        visual.plot_spectrogram(mspec.T, vad = vad)
        visual.subplot(5, 1, 5)
        visual.plot_spectrogram(mfcc.T, vad = vad)
# ====== check if any pitch or f0 allzeros ====== #
indices = sorted([(name, s, e) for name, (s, e) in ds['indices']],
                 key=lambda x: x[1])
for name, start, end in indices:
    pitch = ds['pitch'][start:end][:]
    if not np.any(pitch):
        print("Pitch and f0 of name: %s contains only zeros" % name)
# ====== Visual cluster ====== #
if PCA and False:
    from sklearn.manifold import TSNE
    feat = 'mspec'
    X = []; y = []
    feat_pca = ds[feat + '_pca']
    for f, (start, end) in ds['indices']:
        X.append(
            np.mean(
                feat_pca.transform(ds[feat][start:end]),
                axis=0, keepdims=True)
        )
        y.append(int(f[0]))
    X = np.concatenate(X, axis=0)
    y = np.asarray(y)
    X_ = TSNE(n_components=2).fit_transform(X)
    colors = visual.generate_random_colors(len(set(y)), seed=12082518)
    y = [colors[i] for i in y]
    legend = {c: str(i) for i, c in enumerate(colors)}
    with visual.figure(ncol=1, nrow=5):
        visual.plot_scatter(X[:, 0], X[:, 1], color=y, legend=legend)
    with visual.figure(ncol=1, nrow=5):
        visual.plot_scatter(X_[:, 0], X_[:, 1], color=y, legend=legend)
    # ====== save all the figure ====== #
    visual.plot_save(figpath, tight_plot=True)
    print("Figure saved to:", figpath)
    ds.archive()
    print("Archive at:", ds.archive_path)
