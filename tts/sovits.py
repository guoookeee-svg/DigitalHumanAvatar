import time
import numpy as np
import resampy
import soundfile as sf
import requests
from io import BytesIO
from typing import Iterator

from utils.logger import logger
from .base_tts import BaseTTS, State
from registry import register

@register("tts", "gpt-sovits")
class SovitsTTS(BaseTTS):
    def txt_to_audio(self,msg:tuple[str, dict]):
        text,textevent = msg
        ref_file = textevent.get('tts', {}).get('ref_file',self.opt.REF_FILE)
        ref_text = textevent.get('tts', {}).get('ref_text',self.opt.REF_TEXT)
        self.stream_tts(
            self.gpt_sovits(
                text=text,
                reffile=ref_file,
                reftext=ref_text,
                language="zh", #en args.language,
                server_url=self.opt.TTS_SERVER, #"http://127.0.0.1:5000", #args.server_url,
            ),
            msg
        )

    # ── 流水线接口（配合 base_tts 的句间预取，消除句间静音间隙）──

    def open_stream(self, msg: tuple[str, dict]):
        """预打开合成：POST 立即返回 header（~3ms），body 由服务端生成/排队。
        与当前句的播放并行，句1 播完句2 立即可用。"""
        text, textevent = msg
        ref_file = textevent.get('tts', {}).get('ref_file', self.opt.REF_FILE)
        ref_text = textevent.get('tts', {}).get('ref_text', self.opt.REF_TEXT)
        req = {
            'text': text,
            'text_language': "zh",
            'refer_wav_path': ref_file,
            'prompt_text': ref_text,
            'prompt_language': "zh",
            'top_k': 15,
            'top_p': 1.0,
            'temperature': 1.0,
            'speed': 1.0,
            'inp_refs': [],
            'sample_steps': 32,
            'if_sr': False,
        }
        try:
            res = requests.post(f"{self.opt.TTS_SERVER}/", json=req, stream=True, timeout=30)
            if res.status_code != 200:
                logger.error("gpt_sovits open_stream error:%s", res.text[:200])
                return None
            return res
        except Exception:
            logger.exception('sovits open_stream')
            return None

    def play_stream(self, handle, msg: tuple[str, dict]):
        """消费预打开的 response：流式读 body → 逐帧推给渲染。
        推帧间隙调用 _maybe_prefetch 预取下一句（流水线）。"""
        text, textevent = msg
        start = time.perf_counter()
        first = True
        # 检查用户是否正在说话：若用户在说话，丢弃队列并打断 LLM 避免抢话
        try:
            from server.asr_server import is_user_speaking
            if is_user_speaking():
                logger.info(f"[TTS] 用户正在说话，清空 TTS 队列并打断 LLM")
                self.msgqueue.queue.clear()
                self.parent.llm_generation += 1
                return
        except ImportError:
            pass
        # 立即标记为正在说话（渲染线程来不及更新），确保 VAD 打断可靠
        self.parent.speaking = True
        logger.info(f"[TTS] play_stream start, speaking=True")
        try:
            for chunk in handle.iter_content(chunk_size=None):
                # 打断检查：state=PAUSE 时停止（body 不再消费，连接关闭）
                if self.state == State.PAUSE:
                    logger.info("tts interrupted, stop streaming audio")
                    return
                if not chunk:
                    continue
                if first:
                    logger.info(f"gpt_sovits Time to first chunk: {time.perf_counter()-start}s")
                    first = False
                byte_stream = BytesIO(chunk)
                stream = self.__create_bytes_stream(byte_stream)
                streamlen = stream.shape[0]
                idx = 0
                while streamlen >= self.chunk:
                    eventpoint = {}
                    if first:
                        eventpoint = {'status': 'start', 'text': text}
                        first = False
                    eventpoint.update(**textevent)
                    self.parent.put_audio_frame(stream[idx:idx+self.chunk], eventpoint)
                    streamlen -= self.chunk
                    idx += self.chunk
                    # 流水线：推帧间隙预取下一句（非阻塞，开销极低）
                    self._maybe_prefetch()
            if self.state == State.RUNNING:
                eventpoint = {'status': 'end', 'text': text}
                eventpoint.update(**textevent)
                self.parent.put_audio_frame(np.zeros(self.chunk, np.float32), eventpoint)
        finally:
            try:
                handle.close()
            except Exception:
                pass

    def gpt_sovits(self, text, reffile, reftext,language, server_url) -> Iterator[bytes]:
        start = time.perf_counter()
        # 适配 GPT-SoVITS 官方 api.py 的接口（/ 端点 + refer_wav_path 参数）
        req={
            'text':text,
            'text_language':language,
            'refer_wav_path':reffile,
            'prompt_text':reftext,
            'prompt_language':language,
            'top_k':15,
            'top_p':1.0,
            'temperature':1.0,
            'speed':1.0,
            'inp_refs':[],
            'sample_steps':32,
            'if_sr':False
        }
        try:
            res = requests.post(
                f"{server_url}/",
                json=req,
                stream=True,
                timeout=30,  # 加超时，避免 GPT-SoVITS 卡住导致 process_tts 单线程阻塞
            )
            end = time.perf_counter()
            logger.info(f"gpt_sovits Time to make POST: {end-start}s")

            if res.status_code != 200:
                logger.error("Error:%s", res.text)
                return
                
            first = True
        
            for chunk in res.iter_content(chunk_size=None): #12800 1280 32K*20ms*2
                logger.info('chunk len:%d',len(chunk))
                if first:
                    end = time.perf_counter()
                    logger.info(f"gpt_sovits Time to first chunk: {end-start}s")
                    first = False
                if chunk and self.state==State.RUNNING:
                    yield chunk
            #print("gpt_sovits response.elapsed:", res.elapsed)
        except Exception as e:
            logger.exception('sovits')

    def __create_bytes_stream(self,byte_stream):
        #byte_stream=BytesIO(buffer)
        stream, sample_rate = sf.read(byte_stream) # [T*sample_rate,] float64
        logger.info(f'[INFO]tts audio stream {sample_rate}: {stream.shape}')
        stream = stream.astype(np.float32)

        if stream.ndim > 1:
            logger.info(f'[WARN] audio has {stream.shape[1]} channels, only use the first.')
            stream = stream[:, 0]
    
        if sample_rate != self.sample_rate and stream.shape[0]>0:
            logger.info(f'[WARN] audio sample rate is {sample_rate}, resampling into {self.sample_rate}.')
            stream = resampy.resample(x=stream, sr_orig=sample_rate, sr_new=self.sample_rate)

        return stream

    def stream_tts(self,audio_stream,msg:tuple[str, dict]):
        text,textevent = msg
        first = True
        for chunk in audio_stream:
            # 打断检查：state=PAUSE 时停止发送音频帧（打断不彻底修复）
            if self.state == State.PAUSE:
                logger.info("tts interrupted, stop streaming audio")
                break
            if chunk is not None and len(chunk)>0:          
                #stream = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32767
                #stream = resampy.resample(x=stream, sr_orig=32000, sr_new=self.sample_rate)
                byte_stream=BytesIO(chunk)
                stream = self.__create_bytes_stream(byte_stream)
                streamlen = stream.shape[0]
                idx=0
                while streamlen >= self.chunk:
                    eventpoint={}
                    if first:
                        eventpoint={'status':'start','text':text}
                        first = False
                    eventpoint.update(**textevent) 
                    self.parent.put_audio_frame(stream[idx:idx+self.chunk],eventpoint)
                    streamlen -= self.chunk
                    idx += self.chunk
        # 问题3：打断时（state=PAUSE）不发送 status:end，避免前端误判"已说完"
        if self.state == State.RUNNING:
            eventpoint={'status':'end','text':text}
            eventpoint.update(**textevent) 
            self.parent.put_audio_frame(np.zeros(self.chunk,np.float32),eventpoint)
