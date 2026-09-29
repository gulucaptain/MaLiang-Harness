// Shared deterministic helpers; every backend may use these from its source.
window.motion = Object.freeze({
  clamp: (v,a=0,b=1) => Math.min(b,Math.max(a,v)),
  lerp: (a,b,u) => a+(b-a)*u,
  progress: (t,start,end) => end>start ? Math.min(1,Math.max(0,(t-start)/(end-start))) : (t>=end?1:0),
  smooth: u => { u=Math.min(1,Math.max(0,u)); return u*u*(3-2*u); },
  cycle: (t,period) => ((t%period)+period)%period/period,
  random: (seed,index) => { let x=(seed^Math.imul(index+1,0x9e3779b9))>>>0; x=Math.imul(x^(x>>>16),0x21f0aaad); x=Math.imul(x^(x>>>15),0x735a2d97); return ((x^(x>>>15))>>>0)/4294967296; }
});
window.seededRandom = seed => { let state=seed>>>0; return () => { state=(Math.imul(1664525,state)+1013904223)>>>0; return state/4294967296; }; };
window.setAnimatedSVG = source => {
  if(typeof source !== 'string' || source.length>20000000) throw Error('SVG frame must be a markup string under 20 MB (including embedded images)');
  const doc=new DOMParser().parseFromString(source,'image/svg+xml');
  if(doc.querySelector('parsererror') || doc.documentElement.localName !== 'svg') throw Error('Invalid SVG frame');
  for(const el of doc.querySelectorAll('*')) {
    if(['script','foreignobject','animate','animatetransform','animatemotion','set','style'].includes(el.localName.toLowerCase())) throw Error('Active SVG content is forbidden');
    for(const attr of el.attributes) {
      const key=attr.localName.toLowerCase(), value=attr.value.trim();
      if(key.startsWith('on') || /url\s*\(/i.test(value) || /animation|transition/i.test(key+'='+value)) throw Error('Active SVG attribute is forbidden');
      if(key==='href' && !value.startsWith('#') && !/^data:image\/(png|jpeg|webp);base64,/i.test(value)) throw Error('External SVG resources are forbidden');
    }
  }
  const svg=document.importNode(doc.documentElement,true);
  svg.setAttribute('width',window.scene.spec.width);svg.setAttribute('height',window.scene.spec.height);
  document.body.replaceChildren(svg);
};

void 0;
