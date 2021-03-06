from os import listdir
from os.path import isfile, join
import os
import numpy as np
import madmom

from madmom.audio.filters import LogarithmicFilterbank
from madmom.features.beats import RNNBeatProcessor
#from madmom.features.downbeats import RNNDownBeatProcessor, DBNDownBeatTrackingProcessor
from madmom.features.onsets import SpectralOnsetProcessor, RNNOnsetProcessor, CNNOnsetProcessor
from madmom.audio.stft import ShortTimeFourierTransform
from madmom.audio.signal import FramedSignal, Signal
from madmom.audio.spectrogram import LogarithmicSpectrogram, FilteredSpectrogram, Spectrogram

na = np.newaxis

def crop_image_patches(X, h, w, hstride=1, wstride=1, return_2d_patches=False):
    N, H, W, D =  X.shape
    
    assert(h <= H and w <= W)
    
    num_patches_h = (H - h) // hstride + 1
    num_patches_w = (W - w) // wstride + 1
    
    patches = []
    for h_idx in range(num_patches_h):
        hstart = h_idx * hstride
        
        patches_w = []
        for w_idx in range(num_patches_w):
            wstart = w_idx * wstride
            
            patches_w.append(X[:,hstart:hstart + h, wstart:wstart + w, :])
            
        patches.append(patches_w)
            
    patches = np.array(patches)
    
    patches = patches.transpose(2, 0, 1, 3, 4, 5)
    
    if return_2d_patches:
        return patches.reshape(N, num_patches_h, num_patches_w, h, w, D)
    else:
        return patches.reshape(N, num_patches_h * num_patches_w, h, w, D)

def mean_pool(X, h, w):
    N, H, W, D = X.shape
    
    assert(H % h == 0 and W % w == 0)
    
    NH = H // h
    NW = W // w
    
    return X.reshape(N, NH, h, NW, w, D).mean(axis=(2, 4))

def get_spectrogram(path, filtered=True, window=np.hanning, fft_size=1024, sample_rate=None):
    ''' 
        path: single file path
        filtered: generate FilteredSpectrogram or normal one
        
        return numpy array shaped (Frequencies, Timeframes, Channels)
        (log-spaced (Filtered)Spectrogram from madmom)
    '''
    # sample_rate=None takes original sample_rate
    signal = Signal(path, sample_rate=sample_rate)
    frames = FramedSignal(signal)
    stft = ShortTimeFourierTransform(frames, window=window, fft_size=fft_size)
    spectro = LogarithmicSpectrogram(stft)
    if filtered:
        return FilteredSpectrogram(spectro)
    else:
        return spectro

def get_dir_spectrograms(audio_dir, num_samples = -1):
    '''
        audio_dir: directory path
        num_samples: number of tracks sampled from directory
        
        return numpy array of (Frequencies, Timeframes, Channels)
    '''
    # TODO:
    # add STFT options to the spectrogram (window size etc)
    # add possibility to use different options at the same time (add depth dimension, is there a problem with the resulting shape?)

    audio_files = [f for f in listdir(audio_dir) if isfile(join(audio_dir, f))][:num_samples]

    # calc spectrogram for all files in the folder
    spectrograms = np.array([get_spectrogram(join(audio_dir, af)) for af in audio_files])

    # transform to N, H, W shape
    spectrograms = spectrograms.transpose(0, 2, 1) 
    
    return spectrograms

def get_dataset(music_dir, speech_dir, hpool=16, wpool=15, shuffle=True, num_samples = -1):
    
    music_spectros  = get_dir_spectrograms(music_dir, num_samples)
    speech_spectros = get_dir_spectrograms(speech_dir, num_samples)
    
    X = np.concatenate([music_spectros, speech_spectros], axis=0)[:,:,:,na]
    
    # create labels, 1 for music, -1 for speech
    Y = ((np.arange(X.shape[0]) < music_spectros.shape[0]) - .5) * 2
    
    if hpool > 0 and wpool > 0:
        X = mean_pool(X, hpool, wpool)
    
    if shuffle:
        I = np.random.permutation(X.shape[0])
        
        return X[I], Y[I]
    else:
        return X, Y

def spectro_mini_db_patches(music_dir, speech_dir, patch_width, hpool = 16, wpool = 15, hstride=10, wstride=1, shuffle=True, max_samples = -1):
    
    X, Y = spectro_mini_db(music_dir, speech_dir, hpool=hpool, wpool=wpool, shuffle=False, max_samples = max_samples)
        
    return patch_augment(X, Y, patch_width, shuffle, max_samples)

def patch_augment(X, Y, patch_width, shuffle=True, max_samples = -1):
        
    N, H, W, D = X.shape
    
    pos_idxs = Y > 0
    neg_idxs = np.logical_not(pos_idxs)
    
    # crop patches from the images
    X_patched_pos = crop_image_patches(X[pos_idxs], H, patch_width)
    X_patched_neg = crop_image_patches(X[neg_idxs], H, patch_width)
    
    X_patched_pos = X_patched_pos.reshape(-1, *X_patched_pos.shape[2:])
    X_patched_neg = X_patched_neg.reshape(-1, *X_patched_neg.shape[2:])

    num_pos = X_patched_pos.shape[0]
    
    X_patched = np.concatenate([X_patched_pos, X_patched_neg])
    Y_patched = ((np.arange(X_patched.shape[0]) < num_pos) - .5) * 2
    
    if shuffle:
        I = np.random.permutation(X_patched.shape[0])
        return X_patched[I], Y_patched[I]
    
    else:
        return X_patched, Y_patched

def stride_pad_multiply(signal, multiplier):
    
    if len(signal.shape) == 1:
        signal = signal[:,na]
    
    return (signal[:, na,:] * np.ones(multiplier)[na,:,na]).reshape(signal.shape[0] * multiplier, signal.shape[1])

def mean_pool_signal(signal, factor):
    '''
    mean pool (L, D) signal assuming it is a multiple of factor
    '''

    if len(signal.shape) == 1:
        signal=signal[:,na]
    L, D = signal.shape
    return signal.reshape(L//factor, factor, D).mean(axis=1)

def concatenate_and_resample(signals, sample_down=True):
    '''
    signals: list of signals, all lengths need to be multiples of the smallest length
    '''
    lengths = [len(sig) for sig in signals]
    
    if sample_down:
        min_length = min(lengths)
        resample_factors = [int(leng/min_length) for leng in lengths]
 
        downsampled_signals = [mean_pool_signal(signals[i], resample_factors[i]) for i in range(len(signals))]

        return np.concatenate(downsampled_signals, axis=1) 

    else:
        max_length = max(lengths)
        resample_factors = [int(max_length/leng) for leng in lengths]
 
        upsampled_signals = [stride_pad_multiply(signals[i], resample_factors[i]) for i in range(len(signals))]

        return np.concatenate(upsampled_signals, axis=1) 
     

       # upsampled_signals = [stride_pad_multiply(signals[i], upsample_factors[i]) for i in range(len(signals))]

def save_to_disk(X, y, file_name):
    base = "../data/processed"
    os.makedirs(base, exist_ok=True)
    x_path = join(base, file_name+"_X")
    y_path = join(base, file_name+"_y")
    np.save(x_path, X)
    np.save(y_path, y)

def load_rhythm_db_from_disk(file_name):
    base = "../data/processed"
    x_path = join(base, file_name+"_X.npy")
    y_path = join(base, file_name+"_y.npy")
    return np.load(x_path), np.load(y_path)

processors = [SpectralOnsetProcessor(),
    RNNOnsetProcessor(),
    CNNOnsetProcessor(),
    SpectralOnsetProcessor(onset_method='superflux', fps=200, filterbank=LogarithmicFilterbank, num_bands=24, log=np.log10),
    #RNNDownBeatProcessor(),
    #lambda sig: np.array(RNNBeatProcessor(post_processor=None)(sig)).T
    ]

def rhythm_features_for_signal(signal):
    rhythm_features = [process(signal) for process in processors]
    return concatenate_and_resample(rhythm_features)

def load_and_rhythm_preprocess(audio_dir, num_samples=-1):
    audio_files = [f for f in listdir(audio_dir) if isfile(join(audio_dir, f))]

    if num_samples > 0:
        audio_files = audio_files[:num_samples]

    processed = []
    for file_name in audio_files:
        path = join(audio_dir, file_name)
        signal = Signal(path)
        processed.append(rhythm_features_for_signal(signal))
    return processed

def load_rhythm_feature_db(music_dir, speech_dir, num_samples=-1, reload=False):
    file_name = (music_dir + speech_dir).replace("/", "-").replace(".", "")

    try:
        assert not reload
        return load_rhythm_db_from_disk(file_name)
        print("loaded from disk")
    except (FileNotFoundError, AssertionError):
        print("generate dataset")

    music = load_and_rhythm_preprocess(music_dir, num_samples)
    music_labels = [1] * len(music)
    speech = load_and_rhythm_preprocess(speech_dir, num_samples)
    speech_labels = [-1] * len(music)

    X = np.array(music + speech)
    y = np.array(music_labels + speech_labels)
    perm = np.random.permutation(len(y))
    X, y = X[perm], y[perm]
    print("X", X.shape)
    print("y", y.shape)
    save_to_disk(X, y, file_name)

    return X, y

