# 扩展 MaLiang-Harness

核心只依赖能力契约、作品状态和证据；scene2d / Canvas / SVG 是内置实现。scene2d 的对象函数与变换契约可在 `src/maliang/scene_tools.py` 和 `src/maliang/scene_runtime.js` 中查看。新增工具不必改 Deep Agents 源码，也不必修改现有渲染器。

## 1. 添加工具 API

插件是普通 Python 包，通过 entry point 注册：

```toml
[project.entry-points."maliang.adapters"]
my_tools = "my_tools:register"
```

```python
from pydantic import Field
from maliang.models import StrictModel
from maliang.runtime import Capability

class Generate(StrictModel):
    prompt: str = Field(min_length=1)
    expected_revision: int = Field(ge=0)

def register(context, registry):
    def generate(prompt, expected_revision):
        # 1. 检查当前版本、任务权限、预算，再请求服务。
        # 2. 使用环境变量读取凭证，不写入作品状态或工具返回值。
        # 3. 记录服务名称、模型、请求参数、job ID、实际产物与费用。
        # 4. 将产物保存到 context.store，返回结构化执行结果。
        # 5. 如修改作品，使用 store.mutate(expected_revision, callback)。
        raise NotImplementedError("Implement your provider here")

    registry.register(Capability(
        name="provider_generate",
        description="Describe supported media, parameters and limitations here.",
        schema=Generate,
        handler=generate,
        family="generation",
        mutates=True,
    ))
```

入口函数在 context 初始化时调用；LLM 通过 `list_capabilities` 发现工具。注册的 schema 直接成为模型工具参数。已有 `adapters/assets/` 给出了可用的导入素材与图像生成 API 实现，可据此接入别的提供商。

异步视频服务建议分别注册 `submit` / `poll` / `cancel` / `import_result`，在项目中持久化 job ID，避免断点续跑导致重复付费。目前没有内置远程视频服务调度器。需先实现提供商的幂等与预算规则，再将工具暴露给 agent。

插件 Python 代码是受信任的宿主代码；其权限高于浏览器内的绘制程序。工具 handler 不应调用任意 shell，也不应让模型控制输出路径到项目目录之外。

## 2. 添加渲染后端

实现 `adapters.renderers.Renderer` 契约并注册到 `context.renderers`：

```python
class MyRenderer:
    name = "my_renderer"
    suffix = ".json"
    description = "Describe the source format, units, coordinate system and available functions."
    supports_animation = True

    def validate_source(self, source: str):
        # 校验 source 格式和限制；不能依赖 Canvas。
        ...

    def frames(self, artwork, source, times, output, assets):
        # 按 times 的顺序输出 000000.png, 000001.png, ...
        # 每帧必须与 artwork.spec.width/height 一致。
        # assets 是 ID -> data URL，artwork 中保留对象与时间信息。
        ...

def register(context, registry):
    context.renderers.register(MyRenderer())
```

新任务的 `allowed_backends` 需要包含该后端名称。可以把 source 定义为 SVG、场景 JSON、节点图、Python 绘图参数或提供商工作流描述，不必是 JavaScript。统一的帧输出让缓存、预览、区域检查和 MP4 导出可以复用。

如果工具直接生成完整视频或采用独立项目格式，可先注册专属 capability，保留工具自己的表示，返回产物与来源信息。要让这类产物参与现有交付门控，还需实现其输出导入、证据绑定和 verification 适配；仅注册一个 API 函数不会自动获得这些功能。

## 3. 内置 Canvas 契约

```javascript
function(ctx, t, scene, assets, random) {
  const { width, height } = scene.spec;
  ctx.fillStyle = '#f6f1e6';
  ctx.fillRect(0, 0, width, height);
  // t: 绝对秒数，scene.objects/events: 持久状态。
  // random(): 每帧重置的相同随机序列，用于稳定纹理。
  // assets.logo: 已解码图片，可 ctx.drawImage(assets.logo, ...)。
}
```

采用左上角原点、像素坐标和秒为时间单位。绘制函数每帧重新执行；使用绝对 t，不使用 `requestAnimationFrame`、墙钟、网络请求或依赖前一帧的累积状态。只要工具遵循该约定，就能独立渲染任意关键帧或片段。任意自定义 JavaScript 仍可能不遵守确定性约定，框架不保证所有程序绝对确定。

对象属性和事件表是模型可寻址的数据，由绘制程序读取。原始 Canvas 后端不会自动把 `object.id` 变成绘制对象。scene2d 后端通过独立对象函数建立绑定并执行变换；两者都不会假定声明了对象就已在最终画面中可见。图层顺序、运动与属性映射由对应后端处理。

## 4. 状态和证据规则

- 用 `store.mutate` 提交版本；`expected_revision` 不匹配时拒绝修改。
- 不改写用户规格、既有要求或工具权限。恢复和跨项目复用也保留这些约束。
- 资源保存内容哈希、实际数据与来源。生成素材记录 `provenance.generated = true` 或内置 `source = openai_images`，使跨项目复用能遵守目标任务限制。
- 预览和评审绑定作品 revision。任何作品修改后，旧证据不能用于完成声明。
- 图像反馈使用 `image_result()` 的真实图像内容块。路径只用于产物查找。
- 后端调用、编解码、网络请求需有超时；单个工具内部的大量工作应主动调用 `context.meter.check()`。
- 默认限制覆盖主模型调用、工具、渲染帧、token 和运行时间。插件的额外模型调用需显式记账；实际货币费用需由提供商适配器补充。
