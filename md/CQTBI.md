oAuth2单点登录RESTful API用户登录
单点登录接入API · 获取code获取授权码 Code
接口信息
接口地址：http://szxy.cqtbi.edu.cn/oauth2/v1/auth2orize

请求方式：GET

参数配置
appid: 
sso20240408001
state: 
callback
redirect_uri: 
http://szxy.cqtbi.edu.cn/oauth2/api-docs/access_token.html
生成请求URL 测试请求
请求结果
接口说明
此接口用于获取授权码code，用户授权后会通过回调地址返回code参数。

返回参数：code, state（通过回调地址返回）
用户登出
单点登录接入API · 安全退出单点登录系统
注销单点登录
接口信息
接口地址：http://szxy.cqtbi.edu.cn/oauth2/v1/auth2LoginOut

请求方式：GET

参数配置
access_token: 
请输入access_token
redirect_url: 
http://szxy.cqtbi.edu.cn/logout/callback
生成请求URL 测试请求
请求结果
接口说明
注销单点登录，成功后跳转到指定的回调地址。

注意：redirect_url需要进行UTF-8 URL编码
令牌验证
验证用户令牌的有效性
获取访问令牌 Access Token
接口信息
接口地址：http://szxy.cqtbi.edu.cn/oauth2/v1/access_token

请求方式：GET

参数配置
appid: 
sso20240408001
secret: 
002658359667427995860705420291461
code: 
undefined
生成请求URL 测试请求
请求结果
接口说明
使用授权码code换取访问令牌access_token。

返回数据示例：

{
  "access_token": "8c43dce77fc143dd82b49de159d91943",
  "success": true,
  "errcode": "",
  "refresh_token": "",
  "token_type": "example",
  "expires_in": 3600
}

用户信息
单点登录接入API · 获取当前登录用户的基本信息
获取用户信息
接口信息
接口地址：http://szxy.cqtbi.edu.cn/oauth2/v1/access_user

请求方式：POST

参数配置
access_token: 
请输入access_token
生成请求信息 测试请求
请求结果
接口说明
使用access_token获取用户基本信息。

返回数据示例：

{
  "success": true,
  "userloginid": "00763",
  "userrealname": "XXXX",
  "userdeptid": "100213",
  "userdepaname": "网络安全与信息化处",
  "teacher": "1"
}
          获取用户数据
通过办事大厅登录票据直接获取用户数据  
获取用户信息
接口信息
接口地址：http://szxy.cqtbi.edu.cn/oauth2/v1/getUserDataByTicket

请求方式：POST

参数配置(来源大厅地址参数PORTAL_TICKET)
PORTAL_TICKET: 
请输入PORTAL_TICKET
生成请求信息 测试请求
请求结果
接口说明
使用PORTAL_TICKET获取用户基本信息。

返回数据示例：

{
  "data": {
    "userstate": "在职",
    "test_user": "否",
    "userrealname": "秦XX",
    "userloginid": "教师工号或学生学号",
    "depaname": "二级单位或班级",
    "dept_id": "单位编号或班级编号",
    "teacher_or_student": "1：教师，0学生"
  },
  "signature": "DCE1E6275CA7DDA5B22AC02D62EFD484",
  "success": true,
  "timestamp": 1766629020709
}