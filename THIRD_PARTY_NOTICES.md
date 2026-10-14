# 第三方软件与地图数据

项目原创源码适用根目录 MIT LICENSE。该许可证不改变第三方软件、地图 SDK、接口数据或 OSM 数据的许可。

- 百度地图 SDK 与 Web API：遵循百度地图开放平台服务条款；使用者自行配置具有相应服务权限的 AK，不随源码或样例包分发密钥、POI 缓存及历史任务。
- 路网及水系：© OpenStreetMap contributors，依据 [ODbL](https://www.openstreetmap.org/copyright) 使用；固定快照来源、版本和校验值见离线包 manifest。OSM 数据的再分发不适用源码 MIT 许可证。
- 前端直接依赖 React、Ant Design、ECharts 及其依赖按各发行包 LICENSE/NOTICE 使用；后端直接依赖 FastAPI、HTTPX、NumPy、Shapely、NetworkX、PyProj、ContourPy 等按各发行包许可证使用。完整发行版本以两端锁定依赖及算法包配置为准，各包的许可声明随依赖安装保留。

评审附件仅包含可再分发的 OSM 运行数据及来源说明。界面展示 OSM 派生结果时保留 OpenStreetMap 署名与版权链接。百度 POI 数量与路线结果不构成可自由再分发的数据目录。
