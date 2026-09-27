"""ProviderRegistry — Provider 注册中心（单例模式）。

职责：
1. 管理所有已注册 Provider 实例
2. 按类别/数据类型/优先级查询 Provider
3. 支持运行时动态启用/禁用
4. 支持从 YAML 配置文件加载
5. 支持自动扫描注册
"""

import importlib
import pkgutil
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from app.providers.base.config import ConfigLoader
from app.providers.base.provider import BaseProvider
from app.providers.base.types import ProviderLifecycleStatus


class ProviderRegistry:
    """Provider 注册中心（单例模式）。

    管理所有 Provider 实例的注册、查询、启用/禁用等操作。
    全局唯一实例，通过 ProviderRegistry() 获取。

    Usage:
        registry = ProviderRegistry()
        registry.register(provider_instance)
        providers = registry.get_providers(category="market")
    """

    _instance: Optional["ProviderRegistry"] = None
    _initialized: bool = False

    def __new__(cls) -> "ProviderRegistry":
        """单例模式：确保全局只有一个 Registry 实例。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        """初始化注册中心（仅首次创建时执行）。"""
        if self._initialized:
            return
        self._initialized = True
        self._providers: dict[str, BaseProvider] = {}
        self._categories: dict[str, list[str]] = {}
        self._provider_classes: dict[str, type[BaseProvider]] = {}
        logger.debug("ProviderRegistry initialized")

    # ---- 注册与注销 ----

    def register(self, provider: BaseProvider) -> None:
        """注册 Provider 实例。

        Args:
            provider: BaseProvider 子类实例

        Raises:
            ValueError: 同名 Provider 已注册
        """
        name = provider.name
        if name in self._providers:
            raise ValueError(f"Provider '{name}' is already registered")

        self._providers[name] = provider

        # 按类别索引
        category = provider.category
        if category not in self._categories:
            self._categories[category] = []
        self._categories[category].append(name)

        logger.info(
            f"Provider registered: {name} "
            f"(category={category}, priority={provider.priority})"
        )

    def unregister(self, provider_name: str) -> None:
        """注销 Provider。

        Args:
            provider_name: Provider 名称
        """
        if provider_name not in self._providers:
            logger.warning(f"Provider '{provider_name}' not found for unregister")
            return

        provider = self._providers.pop(provider_name)
        category = provider.category

        if category in self._categories:
            self._categories[category] = [
                n for n in self._categories[category] if n != provider_name
            ]

        logger.info(f"Provider unregistered: {provider_name}")

    def register_class(self, name: str, provider_class: type[BaseProvider]) -> None:
        """注册 Provider 类（用于后续从配置实例化）。

        Args:
            name: Provider 名称标识
            provider_class: Provider 类（非实例）
        """
        self._provider_classes[name] = provider_class
        logger.debug(f"Provider class registered: {name} -> {provider_class.__name__}")

    def get_provider_class(self, name: str) -> type[BaseProvider] | None:
        """按名称获取已注册的 Provider 类（用于配置实例化/热重载）。

        Args:
            name: Provider 名称标识

        Returns:
            Provider 类，未找到返回 None
        """
        return self._provider_classes.get(name)

    # ---- 查询 ----

    def get_provider(self, name: str) -> BaseProvider | None:
        """按名称获取 Provider 实例。

        Args:
            name: Provider 名称

        Returns:
            Provider 实例，未找到返回 None
        """
        return self._providers.get(name)

    def get_providers(
        self,
        category: str | None = None,
        enabled_only: bool = True,
        available_only: bool = False,
    ) -> list[BaseProvider]:
        """按条件查询 Provider 列表（已按优先级排序）。

        Args:
            category: 数据类别过滤（None 表示全部）
            enabled_only: 仅返回启用的 Provider
            available_only: 仅返回可接收请求的 Provider

        Returns:
            按优先级排序的 Provider 列表
        """
        if category:
            names = self._categories.get(category, [])
            providers = [self._providers[n] for n in names if n in self._providers]
        else:
            providers = list(self._providers.values())

        if enabled_only:
            providers = [p for p in providers if p.is_enabled]

        if available_only:
            providers = [p for p in providers if p.is_available]

        # 按优先级排序（数值小的优先）
        providers.sort(key=lambda p: p.priority)
        return providers

    def get_providers_by_priority(self, category: str) -> list[BaseProvider]:
        """获取某类别的 Provider 优先级队列。

        Args:
            category: 数据类别

        Returns:
            按优先级排序的可用 Provider 列表
        """
        return self.get_providers(category=category, enabled_only=True, available_only=True)

    def list_all(self) -> list[dict[str, Any]]:
        """列出所有已注册 Provider 及其状态。

        Returns:
            Provider 信息字典列表
        """
        result = []
        for name, provider in self._providers.items():
            result.append({
                "name": name,
                "category": provider.category,
                "status": provider.status.value,
                "health_state": provider.health_state.value,
                "priority": provider.priority,
                "is_enabled": provider.is_enabled,
                "is_available": provider.is_available,
                "base_url": provider.config.base_url,
            })
        return sorted(result, key=lambda x: (x["category"], x["priority"]))

    def get_categories(self) -> list[str]:
        """获取所有已注册的数据类别。"""
        return list(self._categories.keys())

    def count(self, category: str | None = None) -> int:
        """统计 Provider 数量。

        Args:
            category: 指定类别（None 表示全部）

        Returns:
            Provider 数量
        """
        if category:
            return len(self._categories.get(category, []))
        return len(self._providers)

    # ---- 动态管理 ----

    def enable_provider(self, name: str) -> bool:
        """运行时启用 Provider。

        Args:
            name: Provider 名称

        Returns:
            是否成功启用
        """
        provider = self._providers.get(name)
        if not provider:
            logger.warning(f"Cannot enable: provider '{name}' not found")
            return False

        provider.enable()
        logger.info(f"Provider enabled: {name}")
        return True

    def disable_provider(self, name: str) -> bool:
        """运行时禁用 Provider。

        Args:
            name: Provider 名称

        Returns:
            是否成功禁用
        """
        provider = self._providers.get(name)
        if not provider:
            logger.warning(f"Cannot disable: provider '{name}' not found")
            return False

        provider.disable()
        logger.info(f"Provider disabled: {name}")
        return True

    def update_priority(self, name: str, new_priority: int) -> bool:
        """动态更新 Provider 优先级。

        Args:
            name: Provider 名称
            new_priority: 新优先级值

        Returns:
            是否成功更新
        """
        provider = self._providers.get(name)
        if not provider:
            logger.warning(f"Cannot update priority: provider '{name}' not found")
            return False

        old_priority = provider.config.priority
        provider.config.priority = new_priority
        logger.info(f"Provider priority updated: {name} ({old_priority} -> {new_priority})")
        return True

    # ---- 配置加载 ----

    def load_from_config(
        self,
        config_path: str | Path = "config/providers.yaml",
        provider_classes: dict[str, type[BaseProvider]] | None = None,
    ) -> int:
        """从 YAML 配置文件加载并注册 Provider。

        Args:
            config_path: YAML 配置文件路径
            provider_classes: Provider 名称到类的映射（用于实例化）

        Returns:
            成功注册的 Provider 数量
        """
        try:
            config_data = ConfigLoader.load(config_path)
        except FileNotFoundError as e:
            logger.error(f"Failed to load provider config: {e}")
            return 0

        providers_config = config_data.get("providers", {})
        defaults = config_data.get("defaults", {})
        registered_count = 0

        # 合并已注册的类
        all_classes = {**self._provider_classes, **(provider_classes or {})}

        for category, providers in providers_config.items():
            if not isinstance(providers, dict):
                continue

            for name, raw_config in providers.items():
                if not isinstance(raw_config, dict):
                    continue

                # 解析配置
                config = ConfigLoader.parse_provider_config(
                    name=name,
                    category=category,
                    raw=raw_config,
                    defaults=defaults,
                )

                # 跳过禁用的 Provider
                if not config.enabled:
                    logger.debug(f"Skipping disabled provider: {name}")
                    continue

                # 查找对应的 Provider 类
                provider_class = all_classes.get(name)
                if not provider_class:
                    logger.warning(
                        f"No provider class found for '{name}'. "
                        f"Register the class first or implement the provider."
                    )
                    continue

                # 实例化并注册
                try:
                    provider = provider_class(config)
                    self.register(provider)
                    registered_count += 1
                except Exception as e:
                    logger.error(f"Failed to instantiate provider '{name}': {e}")

        logger.info(f"Loaded {registered_count} providers from config: {config_path}")
        return registered_count

    # ---- 自动发现 ----

    def auto_discover(self, package_path: str = "app.providers") -> None:
        """自动扫描 providers/ 目录，发现并注册所有 Provider 类。

        扫描规则：
        - 目录约定：backend/app/providers/{category}/
        - 文件命名：{provider_name}_provider.py
        - 类命名：{ProviderName}Provider
        - 自动识别所有继承 BaseProvider 的非抽象子类

        Args:
            package_path: 要扫描的包路径
        """
        try:
            package = importlib.import_module(package_path)
        except ImportError as e:
            logger.error(f"Failed to import package '{package_path}': {e}")
            return

        if not hasattr(package, "__path__"):
            logger.error(f"'{package_path}' is not a package")
            return

        discovered_count = 0

        for _importer, modname, ispkg in pkgutil.walk_packages(
            package.__path__, prefix=package.__name__ + "."
        ):
            # 跳过 base 模块和 __init__
            if ".base." in modname or modname.endswith(".__init__"):
                continue

            try:
                module = importlib.import_module(modname)
            except ImportError as e:
                logger.warning(f"Failed to import module '{modname}': {e}")
                continue

            # 扫描模块中的 Provider 类
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, BaseProvider)
                    and attr is not BaseProvider
                    and not getattr(attr, "__abstractmethods__", None)
                ):
                    # 显式命名优先（类属性 provider_key），否则类名转 snake_case
                    provider_name = getattr(attr, "provider_key", "") or self._class_to_snake_case(attr_name)
                    # 移除 _provider 后缀（如果有）
                    if provider_name.endswith("_provider"):
                        provider_name = provider_name[:-9]

                    self.register_class(provider_name, attr)
                    discovered_count += 1
                    logger.debug(f"Discovered provider class: {attr_name} -> {provider_name}")

                    # 别名注册（如 FREDProvider 的 snake_case 推导为 f_r_e_d，
                    # 与 YAML 配置 key 不一致时通过 aliases 兼容）
                    for alias in getattr(attr, "aliases", ()) or ():
                        self.register_class(alias, attr)
                        logger.debug(f"Discovered provider alias: {attr_name} -> {alias}")

        logger.info(f"Auto-discovered {discovered_count} provider classes from '{package_path}'")

    @staticmethod
    def _class_to_snake_case(name: str) -> str:
        """将 PascalCase 类名转换为 snake_case。

        例如：BinanceProvider -> binance_provider
        """
        result = []
        for i, char in enumerate(name):
            if char.isupper() and i > 0:
                result.append("_")
            result.append(char.lower())
        return "".join(result)

    # ---- 生命周期管理 ----

    async def initialize_all(self) -> None:
        """初始化所有已注册的 Provider。"""
        for name, provider in self._providers.items():
            try:
                await provider.initialize()
                provider.set_status(ProviderLifecycleStatus.READY)
                logger.info(f"Provider initialized: {name}")
            except Exception as e:
                logger.error(f"Failed to initialize provider '{name}': {e}")
                provider.set_status(ProviderLifecycleStatus.OFFLINE)

    async def shutdown_all(self) -> None:
        """关闭所有已注册的 Provider。"""
        for name, provider in self._providers.items():
            try:
                await provider.shutdown()
            except Exception as e:
                logger.error(f"Error shutting down provider '{name}': {e}")

    def clear(self) -> None:
        """清空所有注册（主要用于测试）。"""
        self._providers.clear()
        self._categories.clear()
        self._provider_classes.clear()
        logger.debug("ProviderRegistry cleared")

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例实例（主要用于测试）。"""
        cls._instance = None
        cls._initialized = False


__all__ = ["ProviderRegistry"]
