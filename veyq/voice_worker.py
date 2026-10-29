"""Fixed local speech worker. Audio stays in RAM and is discarded on cancellation."""
import json
import sys
import threading
import time
from pathlib import Path


def emit(**value):
    print(json.dumps(value,ensure_ascii=False),flush=True)


def transcribe_samples(model, samples):
    import numpy as np
    if len(samples)<1600 or np.max(np.abs(samples))<0.005: return ''
    segments,_=model.transcribe(samples,language=None,beam_size=3,vad_filter=True,
        condition_on_previous_text=False,temperature=0,task='transcribe')
    return ' '.join(segment.text.strip() for segment in segments).strip()[:30000]


def main():
    from .voice import MODEL_REVISION
    from faster_whisper import WhisperModel
    root=Path(sys.argv[2]).resolve()
    if sys.argv[1]=='prepare':
        from faster_whisper.utils import download_model
        root.mkdir(parents=True,exist_ok=True)
        download_model('base',output_dir=str(root),revision=MODEL_REVISION,use_auth_token=False)
        WhisperModel(str(root),device='cpu',compute_type='int8',local_files_only=True,cpu_threads=4)
        emit(state='ready')
        return
    if sys.argv[1]!='record':raise ValueError('Unknown dictation operation.')
    import numpy as np
    import sounddevice as sd
    model=WhisperModel(str(root),device='cpu',compute_type='int8',local_files_only=True,cpu_threads=4)
    stop=threading.Event();discard=threading.Event()
    audio=bytearray();audio_lock=threading.Lock();overflow=threading.Event()
    def command():
        value=sys.stdin.readline().strip()
        if value!='stop':discard.set()
        stop.set()
    threading.Thread(target=command,daemon=True).start()
    def capture(data,frames,timing,status):
        if status: overflow.set()
        with audio_lock:
            remaining=16000*2*120-len(audio)
            if remaining>0:audio.extend(bytes(data)[:remaining])
            if len(audio)>=16000*2*120:stop.set()
    with sd.RawInputStream(samplerate=16000,channels=1,dtype='int16',callback=capture):
        emit(state='recording',started_at=time.time(),max_seconds=120)
        stop.wait(120)
    if discard.is_set():return
    emit(state='transcribing')
    with audio_lock:samples=np.frombuffer(bytes(audio),dtype=np.int16).astype(np.float32)/32768.0
    audio.clear()
    text=transcribe_samples(model,samples)
    emit(state='completed',text=text,audio_overflow=overflow.is_set())


if __name__=='__main__':
    from .tls import initialize
    initialize()
    try:main()
    except Exception:
        emit(state='error',error='Speech model download failed. Check your connection and retry.' if sys.argv[1:2]==['prepare'] else 'Local dictation failed. Check the microphone and retry.')
        sys.exit(1)
