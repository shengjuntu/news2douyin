# 0.21.0：频道、视图与可追溯的图形雷达

雷达围绕“关注什么”和“怎样看”组织：频道限定问题，视图提供不同观察方式。来源、日期和搜索条件在视图间延续；选中的事件仍能形成选题或进入长期专题。

## 本轮界面

- 综合：保留所有已采集事件，包括尚未抽取完成的材料。
- 外交活动：有会谈、访问或签署动作的材料。这是外交活动线索，动作类型不自动证明官方外交身份。
- 军事冲突线索：有袭击动作的材料；袭击可能是犯罪等行为，不将它直接当作已确认战争。
- 事件列表：重要度、采集渠道、原始媒体、原文/固定版本、专题入口。
- 事件网络：拖拽、缩放、平移、搜索定位、布局切换、邻居高亮；选中节点或边查看依据。可放大到弹层中阅读。
- 国家圆弦图：将国家沿圆周排列，连接明确有行动角色依据的国家对。选择国家或连线后查看对应事件与原句；保留未能确定双边国家的覆盖情况。

国家图有独立的示意数据开关，默认关闭；示意数据使用虚构国家，只解释图形，不进入采集库、图数据库、重要度或内容任务。

## 来源的两种含义

“GDELT GKG / GDELT DOC / WorldNewsAPI”是采集渠道，“Reuters / 其他媒体域名”是报道发布者。同一事件可能包含多个渠道和多个媒体；同一URL也可能经由多个渠道发现。展示以可恢复的采集记录为准，历史记录缺失时显示未记录，不能从来源国或域名猜渠道。

采集源过滤按事件的任一报道命中，混合事件保留其它已记录来源和全部证据。图上的来源筛选因此是事件范围过滤，不是重新计算“仅由某个渠道支持”的图。

## 图的含义

事件网络是已有有据参与者关联/相似动作候选关系的交互展示；它不表示因果，也不自动把群组当作同一事件。

国家圆弦图只使用抽取陈述中的显式国家行动者、对象或共同参与方，且只计状态为 reported 的陈述。人物不凭常识归国；行动发生地点、媒体来源国和GKG全文地点提及不能充当对象国。访问巴黎不能因此变成“某国对法国”的外交关系。

有方向的角色关系使用箭形弦；共同参与关系按无向关系标注。弦宽表示当前筛选范围内有证据的唯一事件记录数，同一对国家的同一事件不因多篇转述重复计数。这不是外交关系好坏、战争烈度、伤亡或武器贸易金额，也不是经过全球实体消歧后的真实事件总量。

没有确定国家对的材料保留在频道列表和事件网络中，不补造国家关系。国家图最多显示40个国家、事件网络最多150个事件，候选分析窗口最多5000个事件；接口与页面注明范围/截断。当前数据仅能支持稀疏国家图，后续若接入GDELT Events，需单独处理Actor1/Actor2、CAMEO国家归属代码、行动代码与日期。

## 数据库浏览器

`falkordb/falkordb:v4.22.1` 配套容器开放数据库端口6379和Browser端口3000；本项目将Browser映射为本机13000，数据库仍为16379。`运行管理 → 图数据库浏览器` 可保存当前电脑能访问的网页地址；该地址与后台redis://数据库连接分别配置。保存入口不会自动探测或部署远程服务。

Browser用于选择命名图、运行Cypher、查看节点和边属性；应用的雷达面向业务分析。投影图可被后台同步重建，Browser手改内容可能被覆盖。原文、事实核对及专题选择仍通过应用保存。

跨机器使用时设置 `NEWS2DOUYIN_FALKOR_BROWSER_BIND` 并配置相应访问控制，网页入口填写实际服务器地址，并在运行 Compose 的 .env 中将 NEWS2DOUYIN_FALKOR_BROWSER_URL 设为相同的外部 origin，以同步 NEXTAUTH_URL / AUTH_URL 登录跳转。修改环境后重新创建容器。Browser 内选择数据库地址时，同容器的数据库为 127.0.0.1:6379，区别于主机16379映射。Compose默认只开放本机；本轮没有在用户服务器或实际Docker环境中验证Browser登录与代理部署。

## 参考文献与实现依据

1. Shneiderman, B. (1996). *The Eyes Have It: A Task by Data Type Taxonomy for Information Visualizations*. [UMD原始报告](https://drum.lib.umd.edu/items/155a868e-fb83-4115-9899-9187ea8c0498)。交互采用先看概览、再缩放/过滤、按需查看证据的结构。
2. Franz et al. (2016). *Cytoscape.js: a graph theory library for visualisation and analysis*. Bioinformatics 32(2):309–311, DOI [10.1093/bioinformatics/btv557](https://doi.org/10.1093/bioinformatics/btv557)。[官方API](https://js.cytoscape.org/)说明拖拽、布局、选择与触屏交互；本轮固定3.33.1，本地打包，保留MIT许可。
3. [D3 Chord官方文档](https://d3js.org/d3-chord)、[有方向布局](https://d3js.org/d3-chord/chord)、[箭形弦](https://d3js.org/d3-chord/ribbon)。本轮固定D3 7.9.0并保留ISC许可。有向和无向关系应分别表达。
4. GDELT (2015). [Mapping Media Geographic Networks: The News Co-occurrence Globe](https://blog.gdeltproject.org/mapping-media-geographic-networks-the-news-co-occurrence-globe/)。它把GKG与Google Arms Globe可视化结合，支持国家、时间、缩放；所画的是新闻中国家共现，不能解释为外交或军事行动。
5. [GDELT Events 2.0字段手册](https://data.gdeltproject.org/documentation/GDELT-Event_Codebook-V2.0.pdf)。事件流含Actor1对Actor2的动作；ActorCountryCode是CAMEO国家归属代码，需明确映射而不能直接当ISO3。当前GKG采集不等于已接入这套事件表。
6. [FalkorDB官方Docker部署](https://docs.falkordb.com/operations/docker)、[Browser项目](https://github.com/FalkorDB/falkordb-browser)。数据库浏览器与业务图各有职责；本项目保持原数据库镜像版本，没有为新图UI升级存储引擎。

前端第三方脚本、本地许可证和下载校验值位于 `src/news2douyin/server/static/vendor`，运行页面不请求第三方CDN。

固定版本构建说明：FalkorDB v4.22.1 的 Dockerfile 默认以 BROWSER_TAG=latest 复制 Browser，不能仅凭数据库版本断言内置Browser的精确版本或最新功能。PORT固定为容器内部3000；AUTH_URL与NEXTAUTH_URL同值用于兼容不同认证构建，不代表本轮已实测远程认证。
