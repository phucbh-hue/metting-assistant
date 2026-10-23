// Float32 [-1,1] -> PCM S16LE, gom thành khối ~100ms (1600 mẫu @16k) rồi đẩy về offscreen.js, kèm mức RMS của khối để
// popup biết tab có thật sự phát ra tiếng hay không.
//
// Chạy trong AudioWorklet chứ không phải ScriptProcessorNode: cuộc họp dài cả tiếng, xử lý audio trên main thread sẽ
// rớt mẫu mỗi khi giao diện bận.
class PCMProcessor extends AudioWorkletProcessor {
  constructor(opts) {
    super();
    this.size = (opts.processorOptions && opts.processorOptions.size) || 1600;
    this.buf = new Int16Array(this.size);
    this.n = 0;
    this.sq = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;                    // luồng im lặng vẫn phải giữ node sống
    for (let i = 0; i < ch.length; i++) {
      const s = Math.max(-1, Math.min(1, ch[i]));
      this.buf[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      this.sq += s * s;
      if (this.n === this.size) {
        const out = this.buf.slice();        // copy: this.buf được tái dùng ngay
        this.port.postMessage({ pcm: out.buffer, rms: Math.sqrt(this.sq / this.size) }, [out.buffer]);
        this.n = 0;
        this.sq = 0;
      }
    }
    return true;
  }
}
registerProcessor('pcm', PCMProcessor);
