# NapCatQQ API接口文档及LANCEDB记忆库搭建方案

## 一、NapCatQQ 完整 API 接口文档（结构化整理）

本文档基于NapCatQQ官方接口汇总整理，涵盖账号、好友、群组、消息、文件、AI、转发分享等全品类接口，明确各接口功能与入参，可直接用于机器人开发对接。详细用例可参考官方文档：https://napcat\.apifox\.cn

### 1\. 账号相关接口

|接口字符串|功能说明|请求参数|
|---|---|---|
|get\_login\_info|获取机器人登录账号信息|无参数|
|get\_status|获取机器人在线运行状态|无参数|
|get\_version\_info|获取NapCat版本信息|无参数|
|bot\_exit|退出机器人登录状态|无参数|
|clean\_cache|清理机器人本地缓存数据|无参数|
|set\_self\_longnick|设置QQ个性签名|longNick: string（个性签名内容）|
|set\_input\_status|设置输入状态（正在输入）|user\_id: 账号ID，event\_type: 状态类型|
|set\_diy\_online\_status|设置自定义在线状态|face\_id: 状态图标ID，face\_type: 图标类型，wording: 状态文案|
|set\_online\_status|设置基础在线状态|状态码参数（区分在线、隐身、忙碌等）|
|set\_qq\_profile|修改QQ个人资料|个人资料相关自定义参数|
|set\_qq\_avatar|更换QQ头像|头像文件相关参数|
|get\_clientkey|获取客户端密钥|无参数|

### 2\. 好友相关接口

|接口字符串|功能说明|请求参数|
|---|---|---|
|get\_friend\_list|获取好友列表|no\_cache: 可选，是否禁用缓存（布尔/字符串）|
|get\_friends\_with\_category|获取带分组的好友列表|无参数|
|send\_private\_msg|发送私聊消息|user\_id: 好友ID，message: 消息内容（字符串/数组），可选扩展参数|
|delete\_msg|撤回已发送消息|message\_id: 消息唯一ID|
|get\_msg|获取指定消息详情|message\_id: 消息唯一ID|
|send\_like|给好友资料点赞|user\_id: 好友ID，times: 点赞次数|
|set\_friend\_add\_request|处理好友添加申请|flag: 申请标识，approve: 是否通过，remark: 备注信息|
|set\_friend\_remark|设置好友备注|user\_id: 好友ID，remark: 备注内容|
|delete\_friend|删除好友|user\_id: 好友ID|
|get\_unidirectional\_friend\_list|获取单向好友列表|无参数|
|friend\_poke|好友戳一戳|user\_id: 好友ID|
|mark\_private\_msg\_as\_read|标记私聊消息为已读|user\_id: 好友ID，time: 标记时间戳|
|get\_friend\_msg\_history|获取私聊历史消息|user\_id: 好友ID，count: 获取条数|
|forward\_friend\_single\_msg|转发单条好友消息|user\_id: 目标好友ID，message\_id: 待转发消息ID|
|get\_profile\_like|获取个人资料点赞记录|无参数|
|fetch\_emoji\_like|获取表情点赞详情|无参数|
|nc\_get\_user\_status|获取用户在线状态|user\_id: 目标用户ID|

### 3\. 群组相关接口

|接口字符串|功能说明|请求参数|
|---|---|---|
|get\_group\_list|获取机器人加入的所有群列表|no\_cache: 可选，是否禁用缓存|
|get\_group\_info|获取基础群信息|group\_id: 群ID，可选 no\_cache 禁用缓存|
|get\_group\_info\_ex|获取群扩展详细信息|group\_id: 群ID|
|send\_group\_msg|发送群消息|group\_id: 群ID，message: 消息内容，可选扩展参数|
|set\_group\_add\_request|处理入群申请|flag: 申请标识，approve: 是否通过，reason: 处理备注|
|set\_group\_kick|踢出群成员|group\_id: 群ID，user\_id: 成员ID，reject\_add\_request: 是否拒绝再次申请|
|set\_group\_ban|禁言指定群成员|group\_id: 群ID，user\_id: 成员ID，duration: 禁言时长|
|set\_group\_whole\_ban|开启/关闭全员禁言|group\_id: 群ID，enable: 开启/关闭状态|
|set\_group\_admin|设置/取消群管理员|group\_id: 群ID，user\_id: 成员ID，enable: 是否开启管理员权限|
|set\_group\_card|设置群成员名片|group\_id: 群ID，user\_id: 成员ID，card: 名片内容|
|set\_group\_name|修改群名称|group\_id: 群ID，group\_name: 新群名|
|set\_group\_leave|退出群聊/解散群组|group\_id: 群ID，is\_dismiss: 仅群主可用，是否解散群|
|set\_group\_special\_title|设置群成员专属头衔|group\_id: 群ID，user\_id: 成员ID，special\_title: 头衔内容|
|get\_group\_member\_info|获取单个群成员信息|group\_id: 群ID，user\_id: 成员ID，可选禁用缓存|
|get\_group\_member\_list|获取群全部成员列表|group\_id: 群ID，可选禁用缓存|
|get\_group\_honor\_info|获取群荣誉信息|group\_id: 群ID，type: 可选，荣誉类型|
|get\_essence\_msg\_list|获取群精华消息列表|group\_id: 群ID|
|set\_essence\_msg|设置消息为精华消息|message\_id: 消息ID|
|delete\_essence\_msg|移除精华消息|message\_id: 消息ID|
|group\_poke|群内戳一戳成员|group\_id: 群ID，user\_id: 目标成员ID|
|mark\_group\_msg\_as\_read|标记群消息为已读|group\_id: 群ID，time: 标记时间戳|
|forward\_group\_single\_msg|转发单条群消息|group\_id: 源群ID，message\_id: 消息ID|
|set\_group\_portrait|设置群头像|group\_id: 群ID，file: 头像文件，cache: 缓存参数|
|\_send\_group\_notice|发送群公告|group\_id: 群ID，content: 公告内容，可选扩展参数|
|\_get\_group\_notice|获取群公告列表|group\_id: 群ID|
|\_del\_group\_notice|删除群公告|group\_id: 群ID，notice\_id: 公告ID|
|get\_group\_at\_all\_remain|获取@全体成员剩余次数|group\_id: 群ID|
|get\_group\_ignore\_add\_request|获取群加群请求忽略列表|无参数|
|get\_group\_ignored\_notifies|获取群通知忽略列表|无参数|
|get\_group\_system\_msg|获取群系统消息|无参数|
|get\_group\_shut\_list|获取群禁言成员列表|group\_id: 群ID|
|set\_group\_remark|设置群备注|group\_id: 群ID，remark: 备注内容|
|set\_group\_sign|设置群签到|group\_id: 群ID|
|send\_group\_sign|发送群签到|group\_id: 群ID|

### 4\. 消息相关接口

|接口字符串|功能说明|请求参数|
|---|---|---|
|send\_msg|通用发送消息（私聊/群聊）|message\_type: 消息类型，user\_id/group\_id: 目标ID，message: 消息内容|
|get\_record|获取语音文件|file: 文件标识，out\_format: 可选，输出格式|
|get\_image|获取图片文件|file: 文件标识|
|can\_send\_image|检测是否可发送图片|无参数|
|can\_send\_record|检测是否可发送语音|无参数|
|get\_file|获取通用文件|file: 文件标识，type: 文件类型|
|\_mark\_all\_as\_read|标记所有消息为已读|无参数|
|ocr\_image|标准图片OCR文字识别|image: 图片资源标识|
|\.ocr\_image|增强版图片OCR识别|image: 图片资源标识|
|get\_recent\_contact|获取最近会话联系人|count: 获取数量|
|send\_poke|通用发送戳一戳|根据会话类型传入对应参数|
|get\_forward\_msg|获取合并转发消息详情|message\_id: 合并消息ID|
|mark\_msg\_as\_read|标记指定消息为已读|消息对应关联参数|

### 5\. 文件相关接口

|接口字符串|功能说明|请求参数|
|---|---|---|
|upload\_group\_file|上传文件到群文件|group\_id: 群ID，file: 文件路径，name: 文件名称，folder: 可选，目标文件夹|
|delete\_group\_file|删除群文件|group\_id: 群ID，file\_id: 文件ID，busid: 文件标识|
|create\_group\_file\_folder|新建群文件文件夹|group\_id: 群ID，name: 文件夹名称|
|delete\_group\_folder|删除群文件文件夹|group\_id: 群ID，folder\_id: 文件夹ID|
|get\_group\_file\_system\_info|获取群文件系统信息|group\_id: 群ID|
|get\_group\_root\_files|获取群根目录文件列表|group\_id: 群ID|
|get\_group\_files\_by\_folder|获取群子目录文件列表|group\_id: 群ID，folder\_id: 文件夹ID|
|get\_group\_file\_url|获取群文件下载链接|group\_id: 群ID，file\_id: 文件ID，busid: 文件标识|
|move\_group\_file|移动群文件位置|group\_id: 群ID，file\_id: 文件ID，target\_dir: 目标目录|
|trans\_group\_file|跨群转发文件|group\_id: 源群ID，file\_id: 文件ID，target\_group\_id: 目标群ID|
|rename\_group\_file|重命名群文件|group\_id: 群ID，file\_id: 文件ID，current\_parent\_directory: 原目录，new\_name: 新名称|
|upload\_private\_file|上传私聊文件|user\_id: 好友ID，file: 文件路径，name: 文件名称|
|get\_private\_file\_url|获取私聊文件链接|文件相关标识参数|
|download\_file|远程下载文件|url: 文件链接，thread\_count: 下载线程数，headers: 请求头数组|

### 6\. AI 相关接口

|接口字符串|功能说明|请求参数|
|---|---|---|
|get\_ai\_characters|获取AI语音角色列表|无参数|
|get\_ai\_record|生成AI语音文件|character: 角色名称，group\_id: 群ID，text: 待转换文本|
|send\_group\_ai\_record|发送群AI语音消息|character: 角色名称，group\_id: 群ID，text: 待转换文本|

### 7\. 转发与分享接口

|接口字符串|功能说明|请求参数|
|---|---|---|
|send\_group\_forward\_msg|发送群合并转发消息|group\_id: 群ID，messages: 消息数组|
|send\_private\_forward\_msg|发送私聊合并转发消息|user\_id: 好友ID，messages: 消息数组|
|send\_forward\_msg|通用发送合并转发消息|messages: 消息数组|
|ArkSharePeer|分享QQ联系人|用户相关标识参数|
|ArkShareGroup|分享QQ群组|群相关标识参数|
|get\_mini\_app\_ark|获取小程序卡片|小程序相关参数|

### 8\. 其他功能接口

|接口字符串|功能说明|请求参数|
|---|---|---|
|get\_cookies|获取账号Cookies|domain: 可选，指定域名|
|get\_csrf\_token|获取CSRF令牌|无参数|
|get\_credentials|获取登录凭证|domain: 可选，指定域名|
|get\_doubt\_friends\_add\_request|获取可疑好友申请|无参数|
|set\_doubt\_friends\_add\_request|处理可疑好友申请|申请相关参数|
|get\_stranger\_info|获取陌生人信息|user\_id: 陌生人ID，可选禁用缓存|
|get\_rkey|获取RKey密钥|无参数|
|click\_inline\_keyboard\_button|点击内联键盘按钮|group\_id、bot\_appid、button\_id、callback\_data、msg\_seq|
|translate\_en2zh|英文翻译中文|text: 待翻译文本|
|create\_collection|创建QQ收藏|收藏内容相关参数|
|get\_collection\_list|获取收藏列表|无参数|
|fetch\_custom\_face|获取自定义表情|表情相关参数|
|nc\_get\_packet\_status|获取数据包运行状态|无参数|
|get\_robot\_uin\_range|获取机器人UIN范围|无参数|
|get\_guild\_list|获取频道列表|无参数|
|get\_guild\_service\_profile|获取频道资料|无参数|
|\_get\_model\_show|获取机型展示信息|无参数|
|\_set\_model\_show|设置机型展示信息|无参数|
|get\_group\_msg\_history|获取群历史消息|group\_id: 群ID，count: 获取条数|
|check\_url\_safely|检测URL安全性|url: 待检测链接|
|\.get\_word\_slices|文本词语切片|content: 待处理文本|
|\.handle\_quick\_operation|处理快速操作指令|context: 上下文对象，operation: 操作对象|
|send\_packet|发送自定义数据包|数据包相关参数|
|set\_msg\_emoji\_like|设置消息表情点赞|消息、表情关联参数|
|get\_online\_clients|获取在线客户端列表|无参数|
