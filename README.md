# TileAtlas

TileAtlas 是本地游戏图像资产工作台。入口 http://127.0.0.1:8914 。点击“装载合成演示”得到6张透明精灵；它们是代码绘制的合成素材。

正常流程：建立图集、上传PNG/JPEG/WebP、编辑精灵、生成图集、下载PNG/UV JSON/逐张重建图像；可保存和恢复项目ZIP。精灵编辑支持透明边界裁剪、阈值、旋转、镜像、最近邻或平滑缩放和源画布归一化轴心。可开关参与打包、检查重复、查看联系表、平铺预览与边界接缝统计。矩形打包使用短边优先的贪心空闲矩形算法，支持可选90度旋转、间距和边缘像素外扩。预览和交付共享布局与渲染器，UV清单按原图角点顺序给出坐标。

最多128个精灵；单图24MB、4096边长、800万像素；单图集128–2048的2次幂边长。不承诺最优装箱，不支持多页、动画轨道、九宫格、网格、SDF或引擎专有导入格式。外扩只复制RGBA边缘，不做透明区颜色传播。源图像与变换参数分别保留，包内重建是处理后源画布，不等于未经变换的原文件。

## 启动

`docker compose up -d --build`。数据放独立命名卷 `solo14-tileatlas_tileatlas_data`；重建容器可保留数据。普通Python运行安装requirements.txt后执行 `python app.py`，可设置PORT与TILEATLAS_DATA_DIR。Dockerfile复用已有图像运算运行时，requirements同时支持Python3.11/3.12。

`/prepare` 是复用自solo_13的自有image_lab图像准备工具，支持照片载入、色彩转换、曝光/焦点等合成、遮罩、局部调整、比较与图像导出。需要从摄影原料提取纹理时，可先处理并下载图像，再上传到图集。此工具保留原有工作流；TileAtlas新增的业务模块是tileatlas/、atlas页面及图集接口，不复用版次/审校/印前业务。

## 源码

`tileatlas/raster.py` 负责透明图像和边界；`packing.py` 负责装箱/渲染/UV/重建；`catalog.py` 负责源素材及修订；`delivery.py` 负责图集与工程包；`demo.py` 负责合成演示；`__init__.py` 负责HTTP与页面入口。`image_lab/` 是自有复用图像准备包。`scripts/count_backend.py`可复算Python有效代码，前端和统计工具不计后台。
