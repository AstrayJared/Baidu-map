/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 浏览器端 AK。API 模式缺少地图配置时只展示错误和结果摘要。 */
  readonly VITE_BAIDU_MAP_AK?: string;
  readonly VITE_ANALYSIS_MODE?: 'api' | 'baidu' | 'hybrid' | 'demo';
  readonly VITE_API_BASE_URL?: string;
}
