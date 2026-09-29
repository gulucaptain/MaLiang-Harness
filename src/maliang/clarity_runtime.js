// Diagnostic only: draw calls do not prove final visibility or semantic legibility.
window.resetClarity = () => { window.clarity = {text_calls:0, tiny_text_calls:0, min_text_px:null, examples:[], basis:'Draw-time estimate; may include occluded/offscreen text. Bitmap text is not measured.'}; };
window.resetClarity();
window.recordTextSize = (text, px) => {
  if (!String(text).trim() || !Number.isFinite(px) || px <= 0) return;
  const c = window.clarity;
  c.text_calls++;
  c.min_text_px = c.min_text_px === null ? px : Math.min(c.min_text_px, px);
  if (px < 12) {
    c.tiny_text_calls++;
    if (c.examples.length < 12) c.examples.push({text:String(text).slice(0,100), estimated_px:Math.round(px*100)/100});
  }
};
for (const method of ['fillText','strokeText']) {
  const original = CanvasRenderingContext2D.prototype[method];
  CanvasRenderingContext2D.prototype[method] = function(text,x,y,maxWidth) {
    const result = original.apply(this,arguments);
    const match = this.font.match(/([\d.]+)px/);
    if (match && this.globalAlpha > 0) {
      const m = this.getTransform();
      // Minimum singular value includes skew, rotation and nonuniform scaling.
      const sum = m.a*m.a+m.b*m.b+m.c*m.c+m.d*m.d;
      const det = m.a*m.d-m.b*m.c;
      const scale = Math.sqrt(Math.max(0,(sum-Math.sqrt(Math.max(0,sum*sum-4*det*det)))/2));
      const measured = this.measureText(text).width;
      const squeeze = maxWidth > 0 && measured > 0 ? Math.min(1,maxWidth/measured) : 1;
      window.recordTextSize(text, Number(match[1])*scale*squeeze);
    }
    return result;
  };
}
window.collectSvgClarity = () => {
  for (const text of document.querySelectorAll('svg text')) {
    const style = getComputedStyle(text), m = text.getScreenCTM();
    if (!m || style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) continue;
    window.recordTextSize(text.textContent, parseFloat(style.fontSize)*Math.min(Math.hypot(m.a,m.b),Math.hypot(m.c,m.d)));
  }
};
