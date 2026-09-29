# MaLiang-Harness project page

Static academic project page for MaLiang-Harness. No build step is required.

## Links

- [Project page](https://gulucaptain.github.io/MaLiang-Harness/)
- [Code](https://github.com/gulucaptain/MaLiang-Harness)
- [Paper (arXiv:2609.34309)](https://arxiv.org/abs/2609.34309)

## Local preview

```sh
python3 -m http.server 8768 --bind 127.0.0.1
```

Open http://127.0.0.1:8768/ .

## Files

- `index.html`: page layout, image magnifier and video playlist.
- `assets/catalog.json`: metadata for 42 selected cases and 45 outputs.
- `assets/catalog.js`: the same metadata loaded by the browser as `window.SHOWCASE`.
- `assets/cases/`: original outputs and lightweight preview images.
- `assets/logo.png` and `assets/icons/`: project logo, resource icons and icon sources.
- `.nojekyll`: allows direct static hosting with GitHub Pages.

When editing metadata, keep `catalog.json` and `catalog.js` in sync. Video ordering is defined in `index.html`. Offline renderer fixtures are shown separately. The Hugging Face Daily Papers link is pending.
