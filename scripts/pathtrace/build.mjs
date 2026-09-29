import { build } from 'esbuild';
import { mkdir, copyFile, writeFile } from 'node:fs/promises';
const target = new URL('../../src/maliang/vendor/pathtrace/', import.meta.url);
await mkdir(target, { recursive: true });
await build({
  stdin: { contents: "export { WebGLPathTracer } from 'three-gpu-pathtracer/src/core/WebGLPathTracer.js'; export { GradientEquirectTexture } from 'three-gpu-pathtracer/src/textures/GradientEquirectTexture.js'; export { RoundedBoxGeometry } from 'three/examples/jsm/geometries/RoundedBoxGeometry.js'; export { RectAreaLightUniformsLib } from 'three/examples/jsm/lights/RectAreaLightUniformsLib.js';", resolveDir: process.cwd() },
  bundle: true, format: 'esm', minify: true,
  outfile: new URL('pathtracer.module.js', target).pathname,
  plugins: [{ name: 'local-three', setup(builder) {
    builder.onResolve({ filter: /^three$/ }, () => ({ path: 'https://maliang.invalid/three.module.min.js', external: true }));
  } }],
});
for (const pkg of ['three', 'three-gpu-pathtracer', 'three-mesh-bvh']) {
  await copyFile(new URL(`node_modules/${pkg}/LICENSE`, import.meta.url), new URL(`${pkg}.LICENSE`, target));
}
await writeFile(new URL('versions.json', target), JSON.stringify({three: '0.180.0', pathtracer: '0.0.24', bvh: '0.9.5'}));
