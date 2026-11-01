// Float32 [-1,1] -> PCM S16LE, gom khối ~100ms (1600 mẫu @16k) rồi đẩy về offscreen.js, kèm ĐƯỜNG BAO NĂNG LƯỢNG
// (RMS mỗi 20ms) để offscreen.js dò vọng âm giữa mic và tab.
//
// Đường bao chứ không phải một số RMS: dò vọng âm cần so hình dạng năng lượng của mic với tab theo thời gian, đủ mịn để
// thấy độ trễ vài chục ms. AudioWorklet chứ không phải ScriptProcessorNode: họp dài cả tiếng, xử lý trên main thread rớt mẫu.
const ENV_SAMPLES = 320;                         // 20ms @16k = một điểm đường bao

class PCMProcessor extends AudioWorkletProcessor {
  constructor(opts) {
    super();
    this.size = (opts.processorOptions && opts.processorOptions.size) || 1600;
    this.buf = new Int16Array(this.size);
    this.n = 0;
    this.env = [];
    this.sq = 0;
    this.k = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;                        // luồng im lặng vẫn phải giữ node sống
    for (let i = 0; i < ch.length; i++) {
      const s = Math.max(-1, Math.min(1, ch[i]));
      this.buf[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      this.sq += s * s;
      if (++this.k === ENV_SAMPLES) { this.env.push(Math.sqrt(this.sq / ENV_SAMPLES)); this.sq = 0; this.k = 0; }
      if (this.n === this.size) {
        const out = this.buf.slice();            // copy: this.buf được tái dùng ngay
        this.port.postMessage({ pcm: out.buffer, env: this.env }, [out.buffer]);
        this.env = [];
        this.n = 0;
      }
    }
    return true;
  }
}
registerProcessor('pcm', PCMProcessor);
