# Technical indicators

The local API calculates indicators from up to 720 stored, closed candles for the requested interval. Calculations use 34-digit decimal arithmetic and reset after a gap. A missing value is returned as JSON `null` until the latest contiguous segment has enough candles. Results include the final candle's close time and the number of candles in that segment.

- RSI uses a 14-change Wilder seed and recurrence. An entirely flat window returns 50.
- ATR uses the first 14 true ranges with a previous close, followed by Wilder smoothing.
- SMA and EMA use 20 closes. EMA starts at the simple average of the first 20 closes.
- MACD uses 12- and 26-period EMAs seeded on the same candle and a nine-period EMA signal. It requires 34 candles.
- Bollinger bands are the 20-close SMA plus or minus two population standard deviations.
- Volatility is the population standard deviation of the last 20 simple candle-to-candle returns. Volume SMA is the average volume of the last 20 candles.

The Windows validation compared RSI, SMA, EMA, MACD and its signal and histogram, ATR, and both Bollinger bounds against TA-Lib 0.8.3 using 100 non-flat synthetic closes. TA-Lib is used as a verification reference. The runtime calculations are implemented in Python. [TA-Lib functions](https://ta-lib.org/functions/)
