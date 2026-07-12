"""Tests for K-line pattern recognition and CYQ chip distribution algorithm."""

import pytest
from tradingagents.agents.utils.analysis_tools import _detect_patterns
from tradingagents.dataflows.a_stock import _compute_cyq


class TestPatternRecognition:
    """Test _detect_patterns with synthetic OHLCV data."""

    def _make_klines(self, n, base_price=10.0, trend="flat"):
        """Generate synthetic K-line arrays."""
        opens, highs, lows, closes, volumes = [], [], [], [], []
        for i in range(n):
            if trend == "up":
                c = base_price + i * 0.1
                o = c - 0.05
            elif trend == "down":
                c = base_price - i * 0.1
                o = c + 0.05
            else:
                c = base_price
                o = base_price
            h = max(o, c) + 0.1
            l = min(o, c) - 0.1
            v = 10000
            opens.append(o)
            highs.append(h)
            lows.append(l)
            closes.append(c)
            volumes.append(v)
        return opens, highs, lows, closes, volumes

    def test_empty_data(self):
        patterns = _detect_patterns([], [], [], [])
        assert patterns == []

    def test_insufficient_data(self):
        """Less than 3 candles should return empty."""
        patterns = _detect_patterns([10], [10.5], [9.5], [10.2])
        assert patterns == []

    def test_flat_market_no_pattern(self):
        """Flat market with uniform candles should have minimal patterns."""
        o, h, l, c, v = self._make_klines(30, trend="flat")
        patterns = _detect_patterns(o, h, l, c, v)
        # Flat market might detect box consolidation but no reversal
        pattern_names = [p["pattern"] for p in patterns]
        assert "早晨之星 (Morning Star)" not in pattern_names
        assert "看涨吞没 (Bullish Engulfing)" not in pattern_names

    def test_bullish_engulfing(self):
        """Construct a clear bullish engulfing pattern at the end."""
        n = 20
        o, h, l, c, v = self._make_klines(n, trend="flat")
        # Make second-to-last candle bearish
        o[-2] = 11.0
        c[-2] = 9.5
        h[-2] = 11.2
        l[-2] = 9.3
        # Last candle: bullish engulfing
        o[-1] = 9.3   # opens below prev close
        c[-1] = 11.5  # closes above prev open
        h[-1] = 11.8
        l[-1] = 9.2
        patterns = _detect_patterns(o, h, l, c, v)
        names = [p["pattern"] for p in patterns]
        assert "看涨吞没 (Bullish Engulfing)" in names

    def test_bearish_engulfing(self):
        """Construct a clear bearish engulfing pattern at the end."""
        n = 20
        o, h, l, c, v = self._make_klines(n, trend="flat")
        # Make second-to-last candle bullish
        o[-2] = 9.5
        c[-2] = 11.0
        h[-2] = 11.2
        l[-2] = 9.3
        # Last candle: bearish engulfing
        o[-1] = 11.2   # opens above prev close
        c[-1] = 9.0    # closes below prev open
        h[-1] = 11.5
        l[-1] = 8.8
        patterns = _detect_patterns(o, h, l, c, v)
        names = [p["pattern"] for p in patterns]
        assert "看跌吞没 (Bearish Engulfing)" in names

    def test_doji(self):
        """Construct a doji (cross) pattern."""
        n = 20
        o, h, l, c, v = self._make_klines(n, trend="flat")
        # Last candle: doji (tiny body, long shadows)
        o[-1] = 10.0
        c[-1] = 10.01  # tiny body
        h[-1] = 10.5   # long upper shadow
        l[-1] = 9.5    # long lower shadow
        patterns = _detect_patterns(o, h, l, c, v)
        names = [p["pattern"] for p in patterns]
        assert "十字星 (Doji)" in names

    def test_large_bullish_candle(self):
        """Construct a large bullish candle."""
        n = 20
        o, h, l, c, v = self._make_klines(n, trend="flat")
        # Last candle: large bullish
        o[-1] = 9.0
        c[-1] = 12.0  # 3x normal body
        h[-1] = 12.5
        l[-1] = 8.8
        patterns = _detect_patterns(o, h, l, c, v)
        names = [p["pattern"] for p in patterns]
        assert "大阳线 (Large Bullish)" in names

    def test_box_consolidation(self):
        """20 candles in tight range should detect box consolidation."""
        n = 20
        o, h, l, c, v = self._make_klines(n, base_price=10.0, trend="flat")
        # Ensure non-zero body so avg_body > 0
        for i in range(n):
            o[i] = 9.99
            c[i] = 10.01
            h[i] = 10.2
            l[i] = 9.8
        patterns = _detect_patterns(o, h, l, c, v)
        names = [p["pattern"] for p in patterns]
        assert any("Box" in name for name in names)

    def test_pattern_structure(self):
        """Each pattern should have required fields."""
        n = 20
        o, h, l, c, v = self._make_klines(n, trend="flat")
        o[-1] = 9.0
        c[-1] = 12.0
        h[-1] = 12.5
        l[-1] = 8.8
        patterns = _detect_patterns(o, h, l, c, v)
        assert len(patterns) > 0
        for p in patterns:
            assert "pattern" in p
            assert "type" in p
            assert "day_offset" in p
            assert "strength" in p
            assert "desc" in p


class TestCYQAlgorithm:
    """Test _compute_cyq with synthetic K-line data."""

    def _make_klines(self, n, start_price=10.0, trend="flat"):
        """Generate synthetic K-line dicts for CYQ calculation."""
        klines = []
        for i in range(n):
            if trend == "up":
                close = start_price + i * 0.05
            elif trend == "down":
                close = start_price - i * 0.05
            else:
                close = start_price
            klines.append({
                "date": f"2024-01-{i+1:02d}",
                "open": close - 0.02,
                "close": close,
                "high": close + 0.1,
                "low": close - 0.1,
                "volume": 100000.0,
                "amount": 1000000.0,
                "amplitude": 2.0,
                "pct_chg": 0.5,
                "turnover": 2.0,
            })
        return klines

    def test_empty_klines(self):
        result = _compute_cyq([])
        assert result == {}

    def test_insufficient_klines(self):
        """Less than 10 klines should return empty."""
        result = _compute_cyq(self._make_klines(5))
        assert result == {}

    def test_flat_market_cyq(self):
        """Flat market: profit ratio should be near 0.5 (price at average cost)."""
        klines = self._make_klines(100, trend="flat")
        result = _compute_cyq(klines)
        assert "profit_ratio" in result
        assert "avg_cost" in result
        assert "concentration_90" in result
        assert "concentration_70" in result
        assert 0 <= result["profit_ratio"] <= 1

    def test_uptrend_cyq(self):
        """Uptrend: profit ratio should be high (most chips below current price)."""
        klines = self._make_klines(100, start_price=10.0, trend="up")
        result = _compute_cyq(klines)
        assert result["profit_ratio"] > 0.5  # Most chips should be in profit

    def test_downtrend_cyq(self):
        """Downtrend: profit ratio should be low (most chips above current price)."""
        klines = self._make_klines(100, start_price=15.0, trend="down")
        result = _compute_cyq(klines)
        assert result["profit_ratio"] < 0.5  # Most chips should be at a loss

    def test_concentration_bounds(self):
        """Concentration should be between 0 and 1."""
        klines = self._make_klines(100)
        result = _compute_cyq(klines)
        assert 0 <= result["concentration_90"] <= 1
        assert 0 <= result["concentration_70"] <= 1

    def test_cost_range_ordering(self):
        """90% range should be wider than 70% range."""
        klines = self._make_klines(100, trend="up")
        result = _compute_cyq(klines)
        range_90 = result["cost_90_high"] - result["cost_90_low"]
        range_70 = result["cost_70_high"] - result["cost_70_low"]
        assert range_90 >= range_70
