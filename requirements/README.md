# Python dependency snapshot / Python 依赖快照

`python312-snapshot.txt` records exact package versions from a historical Python 3.12 environment. It was moved from the root `requirements.lock.txt` without changing its contents.

For normal installation, follow the root README and use the dependency declarations in `pyproject.toml`, then install the vendored Deep Agents package as instructed. This snapshot is not used automatically. It does not include the editable project installations, package hashes, or platform markers, and has not been validated with a fresh installation across platforms. Use it to investigate version differences, not as a complete reproducibility guarantee.

`python312-snapshot.txt` 记录历史 Python 3.12 环境中的具体包版本，由根目录 `requirements.lock.txt` 原样迁入。

常规安装按根目录 README 执行，以 `pyproject.toml` 的依赖声明为准，并按说明安装随仓库提供的 Deep Agents。该快照不会被自动加载；它未包含项目本身的 editable 安装、包哈希或平台标记，也未经过全新环境及跨平台安装验证。保留它用于排查版本差异。
