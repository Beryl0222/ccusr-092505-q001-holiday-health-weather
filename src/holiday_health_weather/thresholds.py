"""风险阈值登记与默认分级。

值班人员可按地区登记自己的阈值（THRESHOLD_REGISTERED）；未单独登记的地区
使用以下默认值。分级数值越大越危险：

* rain（降雨强度 mm/h，越大越危险）：≥10 注意 / ≥25 警示 / ≥50 警告
* low_temp（气温 ℃，越低越危险）：≤8 注意 / ≤5 警示 / ≤0 警告
* fog（能见度 m，越低越危险）：≤1000 注意 / ≤500 警示 / ≤200 警告
"""

from __future__ import annotations

from .model import Cutoff

DEFAULT_THRESHOLD_ID = "default"

DEFAULT_RULES: dict[str, tuple[Cutoff, ...]] = {
    "rain": (
        Cutoff(1, 10.0, "注意"),
        Cutoff(2, 25.0, "警示"),
        Cutoff(3, 50.0, "暴雨警告"),
    ),
    "low_temp": (
        Cutoff(1, 8.0, "注意"),
        Cutoff(2, 5.0, "警示"),
        Cutoff(3, 0.0, "低温警告"),
    ),
    "fog": (
        Cutoff(1, 1000.0, "注意"),
        Cutoff(2, 500.0, "警示"),
        Cutoff(3, 200.0, "浓雾警告"),
    ),
}


def level_for(hazard: str, value: float, rules: dict[str, tuple[Cutoff, ...]]) -> int:
    """根据阈值方向取命中的最高级别。"""

    cutoffs = rules.get(hazard, ())
    level = 0
    for cutoff in cutoffs:
        if hazard == "rain":  # 越大越危险
            hit = value >= cutoff.cutoff
        else:                 # low_temp、fog：越小越危险
            hit = value <= cutoff.cutoff
        if hit:
            level = max(level, cutoff.level)
    return level


def describe(hazard: str, value: float, rules: dict[str, tuple[Cutoff, ...]]) -> str:
    level = level_for(hazard, value, rules)
    name = ""
    for cutoff in rules.get(hazard, ()):
        if cutoff.level == level:
            name = cutoff.name
    return name
