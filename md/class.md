让我梳理 secondclass_tool.py 中用到的所有二课系统 API 接口：

搜索
BASE_URL|\.get\(|\.post\(
深度思考
从代码和 curl 中梳理出二课系统的完整 API 接口文档：

二课系统 API 接口文档
认证接口
① 桥接登录（获取 SSID）
code
复制
插入
新建文件
保存
应用
GET https://2class.cqtbi.edu.cn/Admin/Index/cqtbiSSO?PORTAL_TICKET={ticket}
项	值
方法	GET
参数	PORTAL_TICKET — 门户票据（来自 SSO 登录）
必要 Header	User-Agent: Mozilla/5.0 (Linux; Android 10; K) ... EdgA/149.0.0.0
必要 Header	Referer: http://szxy.cqtbi.edu.cn/ 或 https://2class.cqtbi.edu.cn/Student/Activity/index.html
响应	Set-Cookie: SSID=xxx（需后续请求携带）
注意：必须用安卓 UA 才能拿到有效的 SSID，桌面 UA 虽然返回 200 但 SSID 不可用。

数据接口
② 二课首页 — 活动计数（HTML 切片）
code
复制
插入
新建文件
保存
应用
GET https://2class.cqtbi.edu.cn/Student/My/index.html
Cookie: SSID=xxx
返回格式：HTML，解析目标片段：

html
复制
插入
新建文件
保存
应用
<div class="my_head clearfix">
  <div class="name">沈昱作</div>
  <div class="desc">
    学号：2403740<br>
    学院：电子信息工商学院<br>
    专业：大数据技术<br>
    班级：24大数据3班（学徒制）
  </div>
</div>
<ul class="my_list">
  <li class="my1"><a href="..."><span>219</span>我的活动</a></li>
  <li class="my10"><a href="..."><span>77</span>未签到活动</a></li>
  <li class="my11"><a href="..."><span>56</span>未提交总结的活动</a></li>
  <li class="my2"><a href="..."><span>1</span>我的项目认定</a></li>
  <li class="my12"><a href="..."><span>0</span>我学习的课程</a></li>
  <li class="my3"><a href="..."><span>&nbsp;</span>第二课堂成绩单</a></li>
  <li class="my4"><a href="..."><span>24</span>我的社团</a></li>
</ul>
字段	CSS 选择器	说明
姓名	.my_head .name	文本内容
学号	.my_head .desc	正则 学号：(\d+)
学院	.my_head .desc	正则 学院：(.+)
专业	.my_head .desc	正则 专业：(.+)
班级	.my_head .desc	正则 班级：(.+)
我的活动	li.my1 span	活动总数
未签到活动	li.my10 span	未签到数
未提交总结	li.my11 span	未提交总结数
我的项目认定	li.my2 span	项目认定数
我学习的课程	li.my12 span	课程数
我的社团	li.my4 span	社团数
③ 积分数据（JSON API）
code
复制
插入
新建文件
保存
应用
POST https://2class.cqtbi.edu.cn/Student/My/myScoreTotalGetData.html
Cookie: SSID=xxx
Content-Type: application/x-www-form-urlencoded
X-Requested-With: XMLHttpRequest
请求体（分两种模式）：

模式	Body	说明
总积分	{}（空 body）	返回所有学年的分类积分
学期积分	yearID=20252026&termID=	加 yearID 过滤指定学年
返回格式：JSON（两种结构都有可能）：

返回为数组时（fetch_total_scores 的原始响应）：

json
复制
插入
新建文件
保存
应用
[
  {
    "name": "思想成长",
    "score": 18.5,
    "categoryName": "思想成长",
    "totalScore": 18.5
  },
  {
    "name": "专业技能",
    "score": 22.3,
    "categoryName": "专业技能",
    "totalScore": 22.3
  },
  {
    "name": "职业技能",
    "score": 11.1,
    "categoryName": "职业技能",
    "totalScore": 11.1
  }
]
返回为对象时：

json
复制
插入
新建文件
保存
应用
{
  "totalScore": 51.9,
  "thoughtScore": 18.5,
  "skillScore": 22.3,
  "careerScore": 11.1,
  "sxcz": 18.5,
  "zyjn": 22.3,
  "zyjn2": 11.1
}
字段	说明
totalScore / score / totalScoreSum	总积分
name = "思想成长" / thoughtScore / sxcz	思想成长积分
name = "专业技能" / skillScore / zyjn	专业技能积分
name = "职业技能" / careerScore / zyjn2	职业技能积分
完整数据流
code
复制
插入
新建文件
保存
应用
SSID cookie
    │
    ├─ GET /Student/My/index.html
    │    → HTML 解析 → activity_count, unsigned_count, unfinished_count, club_count
    │    → HTML 解析 → realname, student_id, college, major, deptname
    │
    ├─ POST /Student/My/myScoreTotalGetData.html (body={})
    │    → JSON 解析 → total_score
    │
    └─ POST /Student/My/myScoreTotalGetData.html (body={yearID,termID})
         → JSON 解析 → thought_score, skill_score, career_score
响应格式总结
接口	格式	解析方式
cqtbiSSO	短文本 + Set-Cookie	取 Cookie 中的 SSID
index.html	HTML	BeautifulSoup 切 <li class="myN"><span>
myScoreTotalGetData.html	JSON	r.json() 直接解析
④ 活动列表（JSON API）
POST https://2class.cqtbi.edu.cn/Student/Activity/getActivityCanApply.html
Cookie: SSID=xxx
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
X-Requested-With: XMLHttpRequest
X-Requested-With: XMLHttpRequest
请求参数：
参数	说明	示例
moduleID	模块ID（筛选）	空字符传或其他
typeID	类型ID（筛选）	
keywords	关键词搜索	
sortByTime	按时间排序	desc
sortByScore	按积分排序	desc
响应格式：JSON（数组，每个元素为活动对象）
json
复制
插入
新建文件
保存
应用
[
  {
    "activityID": 121417,
    "activityName": "大学生升学与就业发展咨询",
    "moduleName": "思想成长与价值引领",
    "score": 0.1,
    "organizerName": "24大数据1班",
    "startDate": 1751410800,
    "endDate": 1751423400,
    "img": "178029573888609.jpg",
    "summaryRequired": 0
  }
]
字段	说明
activityID	活动 ID
activityName	活动名称
moduleName	模块名称（思想成长/专业技能等）
score	可获得积分
organizerName	主办方
startDate	开始时间（Unix 时间戳）
endDate	结束时间（Unix 时间戳）
img	活动图片文件名
summaryRequired	是否需要提交总结（1/0）
完整数据流（新增）
SSID cookie
    │
    ├─ GET /Student/My/index.html
    │    → HTML 解析 → activity_count, unsigned_count, ...
    │
    ├─ POST /Student/My/myScoreTotalGetData.html
    │    → JSON → 总积分 + 分类积分
    │
    └─ POST /Student/Activity/getActivityCanApply.html
         → JSON → 可报名活动列表（activityID, name, score, time, organizer）
我的活动（二级页面）分页 API
接口	方法	参数	返回格式
/Student/My/myActivity_End.html	POST	{p, type, maxActivityID}	JSON: [maxID, [activity,...]]
/Student/My/myActivity_other.html	POST	{p, maxActivityID}	JSON: [maxID, [activity,...]]
活动字段（完整）
字段	类型	说明
activityID	int	活动 ID
activityName	string	活动名称
moduleName	string	模块名称
score	float	积分
organizerName	string	主办方
startDate	int	开始时间（Unix）
endDate	int	结束时间（Unix）
img	string	图片文件名
signDate	int/null	签到时间
needSignOut	int	是否需签退（1/0）
signOutDate	int/null	签退时间
summaryRequired	int	是否需总结（1/0）
ifSummary	int/null	是否已提交总结
studentScore	float/null	获取的积分