// scene2d execution contract. Drawing functions own appearance; the harness owns
// object transforms, active intervals, stable layer order and absolute-time tracks.
window.evaluateTrack = function(track, t) {
  const keys = track.keyframes;
  if (t <= keys[0].time) return keys[0].value;
  if (t >= keys[keys.length - 1].time) return keys[keys.length - 1].value;
  for (let i = 1; i < keys.length; i++) {
    if (t <= keys[i].time) {
      const a = keys[i-1], b = keys[i];
      let u = (t-a.time)/(b.time-a.time);
      if (track.interpolation === 'step') u = t === b.time ? 1 : 0;
      if (track.interpolation === 'smooth') u = u*u*(3-2*u);
      return a.value + (b.value-a.value)*u;
    }
  }
};
window.renderScene = async function(t, seed, collectBounds) {
  const {width, height} = window.scene.spec;
  const ctx = window.ctx;
  ctx.canvas.width = width; ctx.canvas.height = height;
  const bounds = {};
  const objects = [...window.scene.objects].sort((a,b) => a.layer-b.layer || a.id.localeCompare(b.id));
  for (const original of objects) {
    if (!original.draw || t < original.start || (original.end !== null && t >= original.end)) {
      bounds[original.id] = null;
      continue;
    }
    const object = structuredClone(original);
    for (const track of object.motion) object.transform[track.property] = window.evaluateTrack(track,t);
    const tr = object.transform;
    const layer = document.createElement('canvas');
    layer.width = width; layer.height = height;
    // Use the same CPU raster path for previews and exports. GPU-backed layers
    // make filter/shadow-heavy scenes expensive to read into the CPU output canvas.
    const local = layer.getContext('2d', {willReadFrequently: true});
    local.translate(tr.x, tr.y);
    local.rotate(tr.rotation*Math.PI/180);
    local.scale(tr.scale_x,tr.scale_y);
    let state = seed >>> 0;
    for (const c of object.id) state = (Math.imul(state,31)+c.charCodeAt(0))>>>0;
    const random = () => {state=(Math.imul(1664525,state)+1013904223)>>>0; return state/4294967296;};
    await window.objectDrawings[object.id](local, t, object, window.assets, random);
    ctx.save();ctx.globalAlpha=tr.opacity;ctx.drawImage(layer,0,0);ctx.restore();
    if (collectBounds && tr.opacity > 0) {
      const pixels=local.getImageData(0,0,width,height).data;
      let left=width,top=height,right=-1,bottom=-1;
      for(let y=0;y<height;y++) for(let x=0;x<width;x++) {
        if(pixels[(y*width+x)*4+3]>0){left=Math.min(left,x);top=Math.min(top,y);right=Math.max(right,x);bottom=Math.max(bottom,y);}
      }
      bounds[object.id]=right<0?null:[left,top,right+1,bottom+1];
    } else bounds[object.id]=null;
  }
  return {time:t, object_bounds:bounds, bounds_basis:'Rendered layer alpha before occlusion; not proof of final visibility'};
};
