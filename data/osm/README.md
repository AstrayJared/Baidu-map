# OSM 离线运行包

当前评审包使用固定上海快照 `geofabrik-shanghai-20260912`，EPSG:32651，步速 1.3 m/s。源码仅跟踪说明；图缓存、边界、风险和障碍层由离线附件交付，不进入 Git。

Map data © OpenStreetMap contributors，数据许可为 [ODbL](https://www.openstreetmap.org/copyright)。数据来源与原始 PBF/各文件 SHA256 见附件 manifest。OSM 是路网模型，不是实地真值。

## 导入与校验

将成果附件 ZIP 及同名 `.sha256` 放在同一目录，从仓库根目录执行：

```powershell
python dev.py setup --sample-package "附件所在目录/shanghai-20260912-runtime.zip"
python dev.py doctor
```

工具只使用 Python 标准库，不需要 Pyrosm；真实运行所需依赖由启动器安装。也可使用 `python backend/scripts/setup_osm_region.py import <runtime.zip>`。`verify <已安装目录>` 重新核对角色、哈希、版本、CRS、步速、覆盖快照与障碍来源；`clear` 只清 OSM 配置，不删文件。

默认安装在 `data/osm/regions/<region>-<archive-hash>/`，路径可迁移或含空格。导入失败不改原环境配置；成功时只更新 OSM 字段并备份 `.env`，不改服务端 AK 或额度。完整体检需要图、边界、风险和障碍层；只有百度成圈不依赖该包。

## 生成附件

维护者可从现有、已验证的一套同版本文件生成附件：

```powershell
python backend/scripts/setup_osm_region.py pack --source data/osm --output output/submission/shanghai-20260912-runtime.zip
```

源目录需有 `shanghai.osm-cache`、`graph_metadata.json`、`shanghai.poly`、`shanghai.risks.geojson`、`shanghai.obstacles.geojson`。`reviews/` 中适用同版本的复核可一并打包。不包含原始 PBF、密钥、百度 POI 或历史体检。

本轮没有在线下载、list/select、多城市自动构建接口。手工构图使用 `prepare_osm_graph.py`，风险/障碍导出使用现有 `tools/export_hybrid_risks.py` 与 `tools/export_hybrid_obstacles.py`；准备依赖见 `backend/requirements-osm-build.txt`。运行阶段不用安装这些准备依赖。

完整评审流程见 [评审启动指南](../../docs/评审启动指南.md)。首次载入与校验路网较慢，服务端在后台预载，界面按真实进度展示；健康检查不代表路网已经加载就绪。
