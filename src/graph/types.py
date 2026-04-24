from dataclasses import field
from typing import Any
from langgraph.graph import MessagesState
from src.prompts.planner_model import Plan
from src.rag.retriever import Resource


class State(MessagesState):
    """
    State 类继承自 MessagesState，用于表示某种状态信息的容器。

    属性:
        research_topic (str): 研究主题，默认为空字符串。
        locale (str): 本地化设置，默认为 "en-US"。
        answer (str): 存储答案或响应内容，默认为空字符串。
        current_plan (Plan | str): 当前计划，可以是 Plan 对象或字符串，默认为 None。
        plan_iterations (int): 计划迭代次数，默认为 0。
        goto (str): 下一步操作的目标节点，默认为 "planner"。
        resources (list[Resource]): 资源列表，使用默认工厂函数初始化为空列表。
        citations (list[dict[str, Any]]): 引用信息列表，使用默认工厂函数初始化为空列表。
    """

    research_topic: str = ""

    locale: str = "zh-CN"

    answer: str = ""

    current_plan: Plan | str = None

    plan_iterations: int = 0

    goto: str = "planner"

    resources: list[Resource] = field(default_factory=list)

    citations: list[dict[str, Any]] = field(default_factory=list)

    # Dynamic replanning and step quality gate state
    current_step_index: int = -1
    replan_count: int = 0
    replan_reason: str = ""
    last_step_status: str = ""  # pass | replan | failed
    step_diagnostics: list[dict[str, Any]] = field(default_factory=list)

    # Iterative ReAct-style micro planning state
    step_count: int = 0
    react_max_steps: int = 3
