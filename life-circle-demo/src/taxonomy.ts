import type { Category } from './types';

export type MajorCategory = 'medical' | 'shopping' | 'education' | 'care' | 'dining' | 'finance' | 'public' | 'leisure' | 'transport' | 'life';

export type BoundaryFlags = {
  freshMarket: boolean;
  hospitalPharmacy: boolean;
  combinedSchool: boolean;
};

export const defaultBoundaryFlags: BoundaryFlags = {
  freshMarket: true,
  hospitalPharmacy: true,
  combinedSchool: true
};

export const boundaryMeta: Record<keyof BoundaryFlags, { label: string; description: string }> = {
  freshMarket: { label: '生鲜超市/门店', description: '是否将生鲜超市、果蔬店等计入菜市场（需求待确认项）' },
  hospitalPharmacy: { label: '医院/门诊药房', description: '是否将医院药房、门诊药房计入药店（需求待确认项）' },
  combinedSchool: { label: '九年一贯制/完小', description: '是否将完小、教学点、九年一贯制小学部计入小学（需求待确认项）' }
};

export const majorMeta: Record<MajorCategory, { label: string; order: number }> = {
  medical: { label: '医疗健康', order: 1 },
  shopping: { label: '购物消费', order: 2 },
  education: { label: '教育', order: 3 },
  care: { label: '疗养康养', order: 4 },
  dining: { label: '餐饮', order: 5 },
  finance: { label: '金融', order: 6 },
  public: { label: '政务与公共服务', order: 7 },
  leisure: { label: '文体休闲', order: 8 },
  transport: { label: '交通出行', order: 9 },
  life: { label: '生活服务', order: 10 }
};

export const majorCategories: MajorCategory[] = Object.keys(majorMeta) as MajorCategory[];

export type DisplayGroup = 'healthcare' | 'education' | 'daily_life' | 'public_mobility' | 'leisure';

export const displayGroupMeta: Record<DisplayGroup, { label: string; order: number; majors: MajorCategory[] }> = {
  healthcare: { label: '健康照护', order: 1, majors: ['medical', 'care'] },
  education: { label: '教育成长', order: 2, majors: ['education'] },
  daily_life: { label: '生活消费', order: 3, majors: ['shopping', 'dining', 'finance', 'life'] },
  public_mobility: { label: '公共出行', order: 4, majors: ['public', 'transport'] },
  leisure: { label: '文体休闲', order: 5, majors: ['leisure'] },
};

export const displayGroups: DisplayGroup[] = Object.keys(displayGroupMeta) as DisplayGroup[];

export function displayGroupForMajor(major: MajorCategory | string): DisplayGroup | undefined {
  return displayGroups.find(group => displayGroupMeta[group].majors.includes(major as MajorCategory));
}

export type SubCategoryDef = {
  key: string;
  label: string;
  major: MajorCategory;
  include: string[];
  exclude?: string[];
  category?: Category;
  boundary?: keyof BoundaryFlags;
  priority?: number;
  negative?: boolean;
};

const educationExclude = [
  '培训', '培训班', '辅导', '辅导班', '补习', '补习班', '教育科技', '教育集团', '教育咨询', '教育服务',
  '课外', '兴趣班', '书院', '私塾', '网校', '在线教育', '美术', '音乐', '舞蹈', '书法', '编程', '机器人',
  '考研', '公考', '留学', '职业技能', '技能培训', '驾驶员培训', '驾校', '体能', '跆拳道'
];

export const subCategories: SubCategoryDef[] = [
  {
    key: 'pharmacy', label: '药店药房', major: 'medical', category: 'pharmacy', priority: 30,
    include: ['药店', '药房', '大药房', '医药商店', '医药连锁', '国药', '药局', '药行', '药品', '医药'],
    exclude: ['兽药', '宠物药', '药膳', '药材种植', '药业', '制药', '药厂', '医药公司', '医药代表', '药品包装']
  },
  {
    key: 'hospital-pharmacy', label: '医院/门诊药房', major: 'medical', category: 'pharmacy', boundary: 'hospitalPharmacy', priority: 36,
    include: ['医院药房', '门诊药房', '住院药房', '便民药房']
  },
  {
    key: 'hospital', label: '医院', major: 'medical', priority: 22,
    include: ['医院', '人民医院', '中医院', '附属医院', '中心医院', '卫生院', '妇幼保健', '儿童医院', '口腔医院', '眼科医院', '肿瘤医院'],
    exclude: ['宠物医院', '动物医院']
  },
  {
    key: 'clinic', label: '社区卫生服务/诊所', major: 'medical', priority: 20,
    include: ['社区卫生服务', '社区卫生服务中心', '社区医院', '诊所', '门诊部', '卫生室', '卫生服务站', '医务室']
  },
  {
    key: 'checkup', label: '体检/健康管理', major: 'medical', priority: 18,
    include: ['体检中心', '体检', '健康管理中心', '健康管理']
  },
  {
    key: 'wet-market', label: '农贸市场', major: 'shopping', category: 'market', priority: 30,
    include: ['菜市场', '农贸市场', '集贸市场', '便民市场', '生鲜市场', '综合市场', '菜篮子', '菜市场', '菜场', '菜市', '市集', '鲜市', '菜店', '副食店', '社区菜店'],
    exclude: ['花鸟市场', '花卉市场', '鲜花市场', '花鸟', '花卉', '鲜花', '家具', '建材', '五金', '汽配', '服装', '农资', '宠物', '批发市场', '电器', '手机', '数码', '古玩', '茶叶市场']
  },
  {
    key: 'fresh-store', label: '生鲜门店', major: 'shopping', category: 'market', boundary: 'freshMarket', priority: 24,
    include: ['生鲜超市', '生鲜', '果蔬', '水果店', '水果', '蔬菜', '水产', '海鲜', '肉铺', '鲜肉', '肉店', '蛋品', '粮油'],
    exclude: ['花卉', '宠物']
  },
  {
    key: 'supermarket', label: '超市/便利店', major: 'shopping', priority: 20,
    include: ['超级市场', '生活超市', '超市', '便利店', '百货', '商场', '购物中心']
  },
  {
    key: 'primary-school', label: '小学', major: 'education', category: 'school', priority: 34,
    include: ['中心小学', '实验小学', '附属小学', '第一小学', '第二小学', '第三小学', '第四小学', '第五小学', '第六小学', '小学'],
    exclude: educationExclude
  },
  {
    key: 'combined-school', label: '九年一贯制/完小/教学点', major: 'education', category: 'school', boundary: 'combinedSchool', priority: 30,
    include: ['九年一贯制', '一贯制', '完小', '教学点', '小学部', '初中部'],
    exclude: educationExclude
  },
  {
    key: 'middle-school', label: '中学', major: 'education', priority: 26,
    include: ['高级中学', '初级中学', '实验中学', '职业中学', '中学', '初中', '高中'],
    exclude: educationExclude
  },
  {
    key: 'college', label: '高等院校', major: 'education', priority: 24,
    include: ['职业技术学院', '职业学院', '高等专科', '研究生院', '大学', '学院'],
    exclude: ['培训学院', '教育学院', '继续教育']
  },
  {
    key: 'preschool', label: '幼儿园/学前', major: 'education', priority: 28,
    include: ['幼儿园', '学前教育', '学前', '幼教', '托育', '保育', '早教中心']
  },
  {
    key: 'training', label: '培训机构', major: 'education', priority: 90, negative: true,
    include: ['培训', '辅导', '补习', '教育科技', '教育集团', '教育咨询', '课外', '兴趣班', '书院', '私塾', '网校', '在线教育', '美术', '音乐', '舞蹈', '书法', '编程', '机器人', '考研', '公考', '留学', '职业技能', '技能培训', '驾驶员培训', '驾校']
  },
  {
    key: 'nursing-home', label: '养老院/敬老院', major: 'care', priority: 30,
    include: ['养老院', '敬老院', '老年公寓', '颐养院', '养老服务中心', '托老所', '日间照料', '长者照护', '养老照料', '福利院'],
    exclude: ['养老地产', '养老咨询', '养老投资', '养老保险']
  },
  {
    key: 'rehab', label: '康复/护理', major: 'care', priority: 28,
    include: ['护理院', '康复中心', '康复医院', '疗养院', '康复理疗', '护理中心', '康复医疗']
  },
  {
    key: 'restaurant', label: '餐厅/快餐', major: 'dining', priority: 16,
    include: ['餐厅', '饭店', '餐馆', '酒楼', '快餐', '小吃', '面馆', '火锅', '食堂', '美食', '大排档', '烧烤', '烤肉', '自助餐']
  },
  {
    key: 'cafe', label: '咖啡/饮品', major: 'dining', priority: 14,
    include: ['咖啡', '奶茶', '饮品', '茶楼', '茶馆', '果汁']
  },
  {
    key: 'bank', label: '银行', major: 'finance', priority: 20,
    include: ['银行', '信用社', '农商行', '储蓄所', '信用合作社']
  },
  {
    key: 'finance-service', label: '保险/证券', major: 'finance', priority: 12,
    include: ['保险', '证券', '基金', '理财', '金融', '投资'],
    exclude: ['养老投资']
  },
  {
    key: 'government', label: '政务/社区服务', major: 'public', priority: 20,
    include: ['人民政府', '街道办', '社区居委会', '居委会', '派出所', '政务服务', '行政服务', '党群服务中心', '社区服务中心', '办事处']
  },
  {
    key: 'post', label: '邮政/快递', major: 'public', priority: 14,
    include: ['邮政', '邮局', '菜鸟驿站', '快递', '物流']
  },
  {
    key: 'library', label: '文化/图书', major: 'public', priority: 14,
    include: ['图书馆', '文化馆', '博物馆', '文化站', '展览馆', '美术馆']
  },
  {
    key: 'park', label: '公园/绿地', major: 'leisure', priority: 16,
    include: ['公园', '广场', '绿地', '游园', '湿地']
  },
  {
    key: 'sports', label: '体育健身', major: 'leisure', priority: 14,
    include: ['健身', '游泳馆', '球馆', '体育场', '体育馆', '运动中心', '瑜伽']
  },
  {
    key: 'entertainment', label: '休闲娱乐', major: 'leisure', priority: 10,
    include: ['电影院', '影城', 'KTV', '网吧', '棋牌', '游戏厅', '娱乐']
  },
  {
    key: 'bus', label: '公共交通', major: 'transport', priority: 20,
    include: ['公交站', '公交车站', 'BRT', '地铁站', '轨道交通', '长途汽车站', '客运站', '火车站']
  },
  {
    key: 'parking', label: '停车场', major: 'transport', priority: 12,
    include: ['停车场', '停车楼', 'P+R']
  },
  {
    key: 'fuel', label: '能源站', major: 'transport', priority: 12,
    include: ['加油站', '充电站', '充电桩', '加气站']
  },
  {
    key: 'repair', label: '维修', major: 'life', priority: 12,
    include: ['手机维修', '家电维修', '锁具', '开锁', '修鞋', '裁缝', '维修']
  },
  {
    key: 'beauty', label: '美容美发/洗衣', major: 'life', priority: 12,
    include: ['理发', '美发', '美容', '美甲', '洗衣', '干洗', '洗护']
  }
];

export const subCategoryByKey: Record<string, SubCategoryDef> = Object.fromEntries(subCategories.map(s => [s.key, s]));
