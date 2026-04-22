本次选用模型为qwen3.5-plus,内置工具包括通晓mcp服务、维基百科搜索api、jina网页爬取等。
1. 前置工作：
配置的API密钥：
DASHSCOPE_API_KEY:sk-7c059df293af4f5d99a7ebe8225ba941
tongxiao_api_key:F02PEzYq8UYrxWRG_82K2v2hzW1wqLY2OTlkOWU2YQ

2. 安装依赖:
pip install -r requirements.txt

3. 运行项目主程序:
python agent.py

4. 对话请求示例（以本地postman为例）：
- 请求URL：http://localhost:8080/process
- 请求方法：POST
- 请求体（JSON格式）：
```json
{
    "question": "你好"
}
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
参考资料：
字节跳动的开源DeepResearchAgent项目 --DeerFlow: https://github.com/bytedance/deer-flow
