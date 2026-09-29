/* Native-size static scenes, all dependencies served from the local worker allowlist. */
void (window.buildPathtraceScene = async function(job, THREE, renderer) {
  const {WebGLPathTracer, GradientEquirectTexture, RoundedBoxGeometry, RectAreaLightUniformsLib} =
    await import('https://maliang.invalid/pathtracer.module.js');
  const doc = JSON.parse(job.source);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(doc.environment.background);
  const env = new GradientEquirectTexture(128);
  env.topColor.set(doc.environment.top); env.bottomColor.set(doc.environment.bottom); env.update();
  scene.environment = env; scene.environmentIntensity = doc.environment.intensity;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = doc.render.exposure;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  RectAreaLightUniformsLib.init();
  const presets = {
    ceramic: {color:'#e8e1d4',roughness:0.23,clearcoat:0.5,clearcoatRoughness:0.18},
    metal: {color:'#d0d2d5',metalness:1,roughness:0.19},
    glass: {color:'#ffffff',transmission:1,roughness:0.03,ior:1.5},
    plastic: {color:'#b65035',roughness:0.3,clearcoat:0.3},
    wood: {color:'#aa7847',roughness:0.55},
    liquid: {color:'#ffffff',transmission:1,roughness:0.04,ior:1.333},
    matte: {color:'#b4ad9f',roughness:0.85},
  };
  const random = seededRandom(job.seed);
  const materials = {};
  for (const data of doc.materials) {
    const props = {...presets[data.preset]};
    for (const k of ['color','roughness','metalness','transmission','ior','clearcoat']) {
      if (data[k] !== null) props[k] = data[k];
    }
    props.attenuationColor = new THREE.Color(data.attenuation_color);
    props.attenuationDistance = data.attenuation_distance;
    props.thickness=1;
    const material = new THREE.MeshPhysicalMaterial(props);
    if (data.texture_asset) {
      if (!assets[data.texture_asset]) throw Error('Unknown texture asset: '+data.texture_asset);
      material.map = new THREE.Texture(assets[data.texture_asset]);
      material.map.colorSpace = THREE.SRGBColorSpace; material.map.needsUpdate = true;
    } else if (data.preset === 'wood') {
      const c = document.createElement('canvas'); c.width=256; c.height=256;
      const ctx=c.getContext('2d'); ctx.fillStyle=props.color; ctx.fillRect(0,0,256,256);
      for(let y=0;y<256;y++) {
        ctx.strokeStyle=`rgba(52,23,6,${0.03+random()*0.14})`; ctx.beginPath();
        for(let x=0;x<=256;x+=4) {
          const py=y+2*Math.sin(x*.025+y*.15)+Math.sin(x*.06+y*.08);
          if(x===0)ctx.moveTo(x,py);else ctx.lineTo(x,py);
        }
        ctx.stroke();
      }
      material.map=new THREE.CanvasTexture(c); material.map.colorSpace=THREE.SRGBColorSpace;
      // The procedural map already includes the base color.
      material.color.set('#ffffff');
    }
    if(material.map) {
      material.map.wrapS=material.map.wrapT=THREE.RepeatWrapping;
      material.map.repeat.set(...data.texture_scale);
    }
    materials[data.id]=material;
  }
  function geometry(g) {
    switch(g.type) {
      case 'sphere': return new THREE.SphereGeometry(g.radius,g.segments,Math.max(8,Math.floor(g.segments/2)));
      case 'box': return new THREE.BoxGeometry(...g.size);
      case 'rounded_box': return new RoundedBoxGeometry(...g.size,3,g.bevel);
      case 'plane': return new THREE.PlaneGeometry(g.size[0],g.size[1]);
      case 'cylinder': return new THREE.CylinderGeometry(g.radius_top,g.radius,g.height,g.segments);
      case 'torus': return new THREE.TorusGeometry(g.radius,g.tube_radius,g.radial_segments,g.segments);
      case 'lathe': return new THREE.LatheGeometry(g.profile.map(p=>new THREE.Vector2(...p)),g.segments);
      case 'tube': return new THREE.TubeGeometry(new THREE.CatmullRomCurve3(g.points.map(p=>new THREE.Vector3(...p)),g.closed,'centripetal'),g.segments,g.tube_radius,g.radial_segments,g.closed);
      case 'extrude': {
        const shape = new THREE.Shape(g.profile.map(p=>new THREE.Vector2(...p)));
        shape.closePath();
        return new THREE.ExtrudeGeometry(shape,{depth:g.height,steps:1,bevelEnabled:g.bevel>0,bevelSegments:3,bevelSize:g.bevel,bevelThickness:g.bevel,curveSegments:12});
      }
      case 'mesh': {
        const geo=new THREE.BufferGeometry();
        geo.setAttribute('position',new THREE.Float32BufferAttribute(g.vertices.flat(),3));
        geo.setIndex(g.indices);geo.computeVertexNormals();return geo;
      }
      default: throw Error('Unsupported geometry: '+g.type);
    }
  }
  for(const data of doc.objects) {
    if(!data.visible)continue;
    const mesh=new THREE.Mesh(geometry(data.geometry),materials[data.material]);
    mesh.name=data.id; mesh.position.set(...data.position);
    mesh.rotation.set(...data.rotation.map(v=>THREE.MathUtils.degToRad(v)));
    mesh.scale.set(...data.scale);scene.add(mesh);
  }
  if(!scene.children.some(o=>o.isMesh))throw Error('Add at least one visible object before rendering');
  for(const data of doc.lights) {
    const light=new THREE.RectAreaLight(data.color,data.intensity,data.width,data.height);
    light.position.set(...data.position);light.lookAt(new THREE.Vector3(...data.target));scene.add(light);
  }
  const camera=new THREE.PerspectiveCamera(doc.camera.fov,job.width/job.height,0.01,30000);
  camera.position.set(...doc.camera.position);camera.lookAt(new THREE.Vector3(...doc.camera.target));
  scene.updateMatrixWorld(true);camera.updateMatrixWorld();
  const raster = job.quality !== 'final' && doc.render.preview_mode === 'raster';
  let tracer;
  if(!raster) {
    if(!renderer.extensions.has('EXT_color_buffer_float'))throw Error('Path tracing requires WebGL 2 EXT_color_buffer_float');
    tracer=new WebGLPathTracer(renderer);
    tracer.renderDelay=0;tracer.fadeDuration=0;tracer.minSamples=1;
    tracer.rasterizeScene=false;tracer.dynamicLowRes=false;
    tracer.tiles.set(2,2);tracer.bounces=doc.render.bounces;
    tracer.transmissiveBounces=12;tracer.stableNoise=true;
    tracer.textureSize.set(512,512);tracer.setScene(scene,camera);
  }
  const requested=job.quality==='final'?doc.render.final_samples:doc.render.preview_samples;
  return {scene,camera, async render() {
    const gl=renderer.getContext();
    if(raster)renderer.render(scene,camera);
    else {
      const start=performance.now();
      while(tracer.samples<requested) {
        if(performance.now()-start>480000)throw Error(`Path tracing timed out (${tracer.samples}/${requested} samples); reduce resolution or samples`);
        tracer.renderSample();
        // Yield so async shader compilation, cancellation and context events can complete.
        await new Promise(resolve=>setTimeout(resolve,0));
        if(gl.isContextLost())throw Error('WebGL context lost during path tracing');
      }
    }
    gl.finish();
    const debug=gl.getExtension('WEBGL_debug_renderer_info');
    return {engine:'three-gpu-pathtracer',quality:job.quality||'preview',mode:raster?'raster':'pathtrace',samples:raster?0:tracer.samples,requested_samples:raster?0:requested,bounces:doc.render.bounces,seed:job.seed,stable_noise:true,gpu:debug?gl.getParameter(debug.UNMASKED_RENDERER_WEBGL):gl.getParameter(gl.RENDERER),objects:doc.objects.filter(o=>o.visible).length};
  }};
});
