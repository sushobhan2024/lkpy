# This file is part of LensKit.
# Copyright (C) 2018-2023 Boise State University.
# Copyright (C) 2023-2025 Drexel University.
# Licensed under the MIT license, see LICENSE.md for details.
# SPDX-License-Identifier: MIT

from __future__ import annotations

import logging
import warnings
from typing import TypeVar

import pandas as pd

from lenskit.data import ItemListCollection
from lenskit.diagnostics import DataWarning
from lenskit.logging import item_progress
from lenskit.metrics._accum import MetricAccumulator

from ._base import Metric, MetricFunction

_log = logging.getLogger(__name__)
K1 = TypeVar("K1", bound=tuple)
K2 = TypeVar("K2", bound=tuple)


class RunAnalysisResult:
    """
    Results of a bulk metric computation.

    Stability:
        Caller
    """

    _list_metrics: pd.DataFrame
    _global_metrics: pd.Series
    _defaults: dict[str, float]

    def __init__(self, lmvs: pd.DataFrame, gmvs: pd.Series, defaults: dict[str, float]):
        self._list_metrics = lmvs
        self._global_metrics = gmvs
        self._defaults = defaults

    def global_metrics(self) -> pd.Series:
        """
        Get the global metric scores.  This is only the results of
        global metrics; it does not include aggregates of per-list metrics.  For
        aggregates of per-list metrics, call :meth:`list_summary`.
        """
        return self._global_metrics

    def list_metrics(self, fill_missing=True) -> pd.DataFrame:
        """
        Get the per-list scores of the results.  This is a data frame with one
        row per list (with the list key on the inded), and one metric per
        column.

        Args:
            fill_missing:
                If ``True`` (the default), fills in missing values with each
                metric's default value when available.  Pass ``False`` if you
                want to do analyses that need to treat missing values
                differently.
        """
        return self._list_metrics.fillna(self._defaults)

    def list_summary(self, *keys: str) -> pd.DataFrame:
        """
        Sumamry statistics for the per-list metrics.  Each metric is on its own
        row, with columns reporting the following:

        ``mean``:
            The mean metric value.
        ``median``:
            The median metric value.
        ``std``:
            The (sample) standard deviation of the metric.

        Additional columns are added based on other options.  Missing metric
        values are filled with their defaults before computing statistics.

        Args:
            keys:
                Identifiers for different conditions that should be reported
                separately (grouping keys for the final result).
        """
        scores = self.list_metrics(fill_missing=True)
        if keys:
            df = scores.groupby(list(keys)).agg(["mean", "median", "std"]).stack(level=0)
            assert isinstance(df, pd.DataFrame)
        else:
            df = scores.agg(["mean", "median", "std"]).T
            df.index.name = "metric"
        return df

    def merge_from(self, other: RunAnalysisResult):
        """
        Merge another set of analysis results into this one.
        """
        for c in self._list_metrics.columns:
            if c in other._list_metrics.columns:
                warnings.warn(f"list metric {c} appears in both merged results", DataWarning)

        for c in self._global_metrics.index:
            if c in other._global_metrics.index:
                warnings.warn(f"global metric {c} appears in both merged results", DataWarning)

        self._list_metrics = self._list_metrics.join(other._list_metrics, how="outer")
        self._global_metrics = pd.concat([self._global_metrics, other._global_metrics])


class RunAnalysis:
    """
    Compute metrics over a collection of item lists composing a run.

    This class now uses :class:`MetricAccumulator` internally to separate
    accumulation from looping, while maintaining the same external interface.

    Args:
        metrics:
            A list of metrics; you can also add them with :meth:`add_metric`,
            which provides more flexibility.

    Stability:
        Caller
    """

    def __init__(self, *metrics: Metric):
        self._accumulator = MetricAccumulator()
        for metric in metrics:
            self._accumulator.add_metric(metric)

    def add_metric(
        self,
        metric: Metric | MetricFunction | type[Metric],
        label: str | None = None,
        default: float | None = None,
    ):
        """
        Add a metric to this metric set.

        Args:
            metric:
                The metric to add to the set.
            label:
                The label to use for the metric's results.  If unset, obtains
                from the metric.
            default:
                The default value to use in aggregates when a user does not have
                recommendations. If unset, obtains from the metric's ``default``
                attribute (if specified), or 0.0.
        """
        self._accumulator.add_metric(metric, label, default)

    @property
    def metrics(self) -> list:
        """
        The list of metrics to compute.

        .. deprecated:: 2025.4
            Access metrics through the accumulator interface instead.
        """
        return self._accumulator.metrics

    def compute(
        self, outputs: ItemListCollection[K1], test: ItemListCollection[K2]
    ) -> RunAnalysisResult:
        """
        Deprecated alias for :meth:`measure`.

        .. deprecated:: 2025.1.1
            Use :meth:`measure` instead.
        """
        return self.measure(outputs, test)

    def measure(
        self, outputs: ItemListCollection[K1], test: ItemListCollection[K2]
    ) -> RunAnalysisResult:
        """
        Measure a set of outputs against a set of test data.

        This method now uses the MetricAccumulator internally, which provides
        better separation of concerns and supports external evaluation loops.
        """
        self._accumulator._validate_setup()

        n = len(outputs)
        _log.debug("measuring %d metrics for %d output lists", len(self._accumulator.metrics), n)

        no_test_count = 0
        with item_progress("Measuring", n) as pb:
            for key, out in outputs:
                list_test = test.lookup_projected(key)
                if out is None:
                    pass
                elif list_test is None:
                    no_test_count += 1
                else:
                    key_kwargs = dict(zip(outputs.key_fields, key))
                    self._accumulator.measure_list(out, list_test, **key_kwargs)
                pb.update()

        if no_test_count:
            _log.warning("could not find test data for %d lists", no_test_count)

        list_results = self._accumulator.list_metrics(fill_missing=False)
        global_results = self._accumulator.summary_metrics()["mean"]
        defaults = {
            wrapper.label: wrapper.default
            for wrapper in self._accumulator.metrics
            if wrapper.default is not None
        }

        return RunAnalysisResult(list_results, global_results, defaults)  # type: ignore
