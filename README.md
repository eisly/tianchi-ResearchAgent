# 1.前置工作：

配置的API密钥：请前往[阿里云百炼大模型平台](https://bailian.console.aliyun.com/cn-beijing/?spm=5176.12818093_47.resourceCenter.1.3be916d0BYTTfK&tab=model#/model-market)获取阿里云api-key，获取后存储至本地的系统变量（高级系统变量/环境变量/系统变量/添加变量，变量名起名为DASHSCOPE_API_KEY）

安装依赖:

```
pip install -r requirements.txt
```



# 2.启动服务

通过以下指令启动该项目的基础后端agent服务：

```
python main.py
```

1. 对话请求示例（以本地postman为例）：

- 请求URL：http://localhost:8080/process
- 请求方法：POST
- 请求体（JSON格式）：
```json
{"id": 80, "question": "有一位中国歌手，其演唱会风格独特，以数字作为自己的艺名，常喜欢听观众合唱自己的歌而自己却充当指挥，其演唱代表作之一时有一个双指遮住双眼的招牌动作。请问该歌手的本名是？"}
该数据从data/question.jsonl中获取
```
- 响应示例：
```json
event:Ping
...
event: Message
{
    "answer": "你好"
}
```
启动前端服务：

```
1.cd fronted
2.npm install
3.npm run dev
```



# 3.参考项目

字节跳动的开源DeepResearchAgent项目 --DeerFlow: https://github.com/bytedance/deer-flow
