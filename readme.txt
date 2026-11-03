视频字幕生成器 使用说明
============================================================

一、环境要求
------------------------------------------------------------
- Windows 10 / 11
- Python 3.12（不要用 3.14，ctranslate2 还没有对应轮子）
- ffmpeg 已安装并加入 PATH
- NVIDIA 显卡，建议 16 GB 显存（本项目在 RTX 5060 Ti 16G 上验证）
- 纯 CPU 也能跑，但速度会慢很多

二、首次安装
------------------------------------------------------------
1) 创建虚拟环境
   py -3.12 -m venv .venv

2) 安装依赖
   .venv\Scripts\python.exe -m pip install gradio faster-whisper "av>=11,<16" httpx huggingface_hub

   注意：av 必须低于 16。PyAV 16 移除了 faster-whisper 要用的
   metadata_errors 参数，装高了会报 TypeError。

3) 下载模型（本项目已下载好，可跳过）
   .venv\Scripts\python.exe scripts\download_models.py --all

   翻译模型：models\Qwen3-14B-Q4_K_M.gguf（约 8.4 GB）
   识别模型：models\faster-whisper-large-v3\（约 2.9 GB）
   默认走 hf-mirror，国内网络可直接下载；
   海外网络可加参数 --endpoint https://huggingface.co

三、启动
------------------------------------------------------------
最简单：双击 启动字幕工具.bat
   它会先检查 18765 端口，翻译服务没开就自动拉起，然后打开
   http://127.0.0.1:7860

手动启动（两个窗口）：
   powershell -ExecutionPolicy Bypass -File scripts\start_llama.ps1
   .venv\Scripts\python.exe app.py

注意：启动后那个最小化的 llama-server 窗口不要关，它就是翻译服务。

四、修改端口
------------------------------------------------------------
1. 翻译服务端口（llama-server，默认 18765）
   临时改：scripts\start_llama.ps1 -Port 18000
   永久改：
     a) 改 config.py 第 20 行默认值 http://127.0.0.1:18765
     b) 改 scripts\start_llama.ps1 第 2 行的 $Port = 18765
     c) 改 启动字幕工具.bat 里检测端口的数字（第 8 行）
   或者不改文件，直接设环境变量：
     set SUBS_LLAMA_URL=http://127.0.0.1:18000

2. 界面端口（Gradio，默认 7860）
   改 app.py 文件末尾 launch(...) 里的 server_port=7860

注意：服务端口和界面端口是两回事。如果只改了 llama-server 端口，
却没改 config.py，界面会显示「连不上 llama-server」。

五、目录说明
------------------------------------------------------------
models\        模型文件（体积大，已在 .gitignore 中排除）
output\        生成的字幕 .srt；output\_work\ 是抽出来的临时音频
tools\llama\   llama.cpp 可执行文件（CUDA 13.4 版）
core\          音频、识别、翻译、字幕、流水线
scripts\       模型下载、服务启动
app.py         Gradio 界面
config.py      路径、断句阈值、批量大小等配置

六、常见问题
------------------------------------------------------------
- 提示找不到 ffmpeg
  安装 ffmpeg 并加入 PATH，或执行 pip install imageio-ffmpeg

- 界面显示「连不上 llama-server」
  服务没启动或端口不一致，先跑 scripts\start_llama.ps1

- 翻译服务正在加载模型
  第一次启动要十几秒读 8.4 GB 的 gguf，稍等后点「刷新服务状态」

- 识别很慢
  看界面「识别设备」是否显示 CPU；GPU 不可用会自动退回 CPU

- 显存不够
  把 start_llama.ps1 里的 -ngl 99 调小（比如 30），或者换更小的
  Q4 模型；16 GB 显存建议不要开超过 -np 2

- 字幕断句太碎或太长
  调 config.py 里的 MAX_CUE_CHARS / MAX_CUE_SECONDS / MIN_CUE_SECONDS

七、可调环境变量
------------------------------------------------------------
SUBS_LLAMA_URL      翻译服务地址，默认 http://127.0.0.1:18765
SUBS_LLAMA_MODEL    GGUF 模型路径
SUBS_WHISPER_MODEL  Whisper 模型目录
SUBS_MAX_CHARS      每条中文字幕字数上限，默认 16
SUBS_BATCH_SIZE     每批翻译条数，默认 8
SUBS_CONTEXT_SIZE   带上的上文条数，默认 2

八、整体复制到其他电脑
------------------------------------------------------------
模型和 llama.cpp 都放在项目目录内（models\ 约 11.3 GB，tools\ 约 0.6 GB），
没有软链接，整个文件夹拷过去就能用，不需要重新下载。

步骤：
1) 拷贝前先删掉 .venv 目录
   虚拟环境绑定的是本机 Python 安装路径，拷到别的电脑上一定跑不起来。
   同一台电脑换目录则可以保留。

2) 目标电脑安装 Python 3.12 和 ffmpeg，并加入 PATH

3) 在新目录里重建环境
   py -3.12 -m venv .venv
   .venv\Scripts\python.exe -m pip install gradio faster-whisper "av>=11,<16" httpx huggingface_hub

4) models\ 和 tools\ 原样拷贝，不用改 config.py，路径是按项目根目录算的

5) 目标电脑需要较新的 NVIDIA 驱动（能支持 CUDA 12/13）
   llama.cpp 已自带 CUDA 运行库，Whisper 用的是 ctranslate2；
   若报 cudnn / cublas 相关的 DLL 错误，升级显卡驱动即可

6) 双击 启动字幕工具.bat 验证

没有 NVIDIA 显卡时：
   - 把 scripts\start_llama.ps1 里的 -ngl 99 改成 -ngl 0（走 CPU）
   - Whisper 会自动退回 CPU，界面「识别设备」会显示出来，速度会慢很多

九、依赖清单
------------------------------------------------------------
gradio>=5.0.0
faster-whisper>=1.1.0
av>=11,<16
httpx>=0.27.0
huggingface_hub>=0.26.0

安装命令见第二节。
