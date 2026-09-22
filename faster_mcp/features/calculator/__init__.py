"""计算器功能模块。

loader.py 会把这个子包当作一个功能模块导入。必须在这里导出 Feature 子类，
导出方式决定 loader 能否发现它——这就是"一个目录 = 一个功能"的约定。

加新功能时复制本文件的形态即可：
    from .feature import YourFeature

新功能不要照抄 calculator 的名字，也不要把这个包改造成通用模板——
功能之间应当彼此独立，公共代码放 features/base.py 或各自的子模块。
"""

from .feature import CalculatorFeature

__all__ = ["CalculatorFeature"]
