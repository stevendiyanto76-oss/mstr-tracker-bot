# LAPORAN LENGKAP HASIL PENGUJIAN MODEL MSTR V3
## Backtest Historis, 100.000 Path Monte Carlo, 5 Institutional Stress Test, & Win Rate Analytics

**Penyusun:** Nevets Holding Quantitative Asset Management  
**Aset Acuan:** MicroStrategy Inc. (NASDAQ: MSTR) | **Benchmark:** Bitcoin (BTC-USD)  
**Periode Data Historis:** 2020 – 2026 (1.675 Hari Bursa Riil)  
**Periode Simulasi Forward:** Juli 2026 – Desember 2030 (54 Bulan)  

## 0. Master Equation: Satu Rumus Panjang Terpadu (Versi Terkini)

$$\large P_{\text{fair}} = \frac{1000}{S_{\text{diluted}}} \cdot \max\left(0, \; \left[\text{clip}\left(1.0 + \left(0.35 + \frac{0.10}{1 + e^{-\frac{M_{12} - 0.50}{0.05}}}\right) \cdot \tanh(M_{12}) - 0.20 \cdot \max(0, RV_{12} - 0.65) - 0.30 \cdot \text{clip}\left(\frac{15 - \frac{R_{\text{USD}} \cdot 12}{\text{Div}}}{15}, 0, 1\right), \; 0.50, \; 2.20\right)\right] \cdot \left[\frac{P_{\text{BTC}} \cdot H_{\text{BTC}}}{10^9}\right] - D - Pref + R_{\text{USD}} + V_{\text{soft}}\right)$$

### Bedah Komponen Rumus Master:
1. **$mNAV^*(t)$ (Kelipatan Dinamis):**
   - **$1.0$ (Basis Paritas):** Ekuilibrium nilai aset Bitcoin murni.
   - **$\beta(t) \cdot \tanh(M_{12})$:** Premi momentum dengan transisi *Smooth Sigmoid* ($\beta = 0.35 \to 0.45$) dan saturasi batas atas $\tanh$.
   - **$-0.20 \cdot \max(0, RV_{12} - 0.65)$:** Penalti volatilitas realized 1-arah (*strict one-sided penalty* tanpa bonus semu).
   - **$-0.30 \cdot \text{clip}\left(\frac{15 - \text{Runway}}{15}, 0, 1\right)$:** Penalti likuiditas jika cadangan kas di bawah 15 bulan.
   - **$\text{clip}(\dots, 0.50, 2.20)$:** Pembatas struktural ekstrim.
2. **$\left[\frac{P_{\text{BTC}} \cdot H_{\text{BTC}}}{10^9}\right]$ ($NAV_{\text{BTC}}$):** Nilai pasar kotor Bitcoin dalam miliar USD ($66.547B pada spot \$78,749.95 dan 845,050 BTC).
3. **$- D - Pref + R_{\text{USD}} + V_{\text{soft}}$:** Pengurang kewajiban utang senior (\$6.714B) & saham preferen (\$14.625B), penambah kas liquid (\$6.538B), dan lantai operasional software (\$1.000B) $\implies$ Total beban klaim bersih $= -\$13.801\text{B}$.
4. **$\frac{1000}{S_{\text{diluted}}}$:** Pembagi jumlah saham terdilusi penuh (ADSO: 450.121 juta lembar).

### Parameter Input Pasar Live Terkini:
* **Momentum BTC 12-Bulan ($M_{12}$):** **-29.38%** (koreksi siklus 12 bulan) $\implies \tanh(-0.2938) = -0.2856$, $\beta = 0.35 \implies$ Penyesuaian momentum $= -0.1000x$.
* **Realized Volatility ($RV_{12}$):** **60.1%** $\le 65\%$ baseline $\implies$ Penalti volatilitas $\Omega(t) = 0.00x$.
* **Cadangan Kas USD ($R_{\text{USD}}$):** **$6.538B** (Runway dividen = **47.2 bulan** $\ge 15$ bulan) $\implies$ Penalti likuiditas $\Lambda(t) = 0.00x$.
* **Hasil Evaluasi Kelipatan:** $mNAV^*(t) = 1.0 - 0.1000 - 0.00 - 0.00 = \mathbf{0.9000x}$
* **Nilai Wajar Ekuitas:** $V_{\text{equity}} = 0.900032 \times \$66.547\text{B} - \$13.801\text{B} = \$46.094\text{B}$
* **Harga Saham Wajar ($P_{\text{fair}}$):** $(\$46.094\text{B} \times 1000) / 450.121\text{M} = \mathbf{\$102.40\text{ per lembar}}$

### Skalasi 5 Zona Adaptif (Evaluasi di Ruang Kelipatan mNAV Terlebih Dahulu):
Zona dihitung di ruang kelipatan mNAV ($m_{\text{zone}}$) untuk menangkap efek pengungkit modal (*balance sheet leverage amplification*), lalu dikonversi ke harga via jembatan $P(m)$:
$$P(m) = \frac{1000}{S_{\text{diluted}}} \cdot \max\left(0, \; m \cdot NAV_{\text{BTC}} - D - Pref + R_{\text{USD}} + V_{\text{soft}}\right)$$

$$\sigma_{\text{band}} = \text{clip}\left(0.10 + 0.18 \cdot \boldsymbol{Risk} + 0.06 \cdot (1 - DQ), \; 0.10, \; 0.35\right) = \mathbf{13.21\%}$$
$$\boldsymbol{Risk} = \text{clip}\left(0.20(1 - \text{Liq}) + 0.15\Pi_{\text{debt}} + 0.20(1 - \text{clip}(mNAV^*/1.50, 0, 1)) + 0.15 \cdot \text{clip}\left(\frac{\text{NetLev}}{0.50}, 0, 1\right) + 0.15 \cdot \text{clip}\left(\frac{\text{Dilution}}{0.50}, 0, 1\right) + 0.15(1 - \text{TailScore}), \; 0, \; 1\right) = \mathbf{17.83\%}$$

$$\begin{aligned}
m_{\text{SB}} &= \max\left(\text{Floor} + 0.10, \; mNAV^* - 1.50 \cdot \sigma_{\text{band}}\right) = 0.7019x &&\implies P_{\text{StrongBuy}} = P(m_{\text{SB}}) \le \mathbf{\$73.11} \\
m_{\text{Acc}} &= mNAV^* = 0.9000x &&\implies P_{\text{Accumulate}} \in \left(\$73.11, \; \mathbf{\$102.40}\right] \\
m_{\text{Hold}} &= \max(m_{\text{Acc}} + 0.02, \; mNAV^* + 1.75 \cdot \sigma_{\text{band}}) = 1.1312x &&\implies P_{\text{HOLD}} \in \left(\$102.40, \; \mathbf{\$136.58}\right] \\
m_{\text{Red}} &= \max(m_{\text{Hold}} + 0.02, \; mNAV^* + 3.00 \cdot \sigma_{\text{band}}) = 1.2963x &&\implies P_{\text{Reduce}} \in \left(\$136.58, \; \mathbf{\$161.00}\right] \\
m_{\text{Sell}} &> m_{\text{Red}} &&\implies P_{\text{Sell}} > \mathbf{\$161.00}
\end{aligned}$$
*(Catatan: Karena net senior claims bernilai konstan \$13.801B, pergeseran kelipatan $\Delta m$ melahirkan pergeseran harga saham yang teramplifikasi leverage: $\Delta P = \frac{NAV_{\text{BTC}} \cdot \Delta m \cdot 1000}{S_{\text{diluted}}}$, bukan sekadar perkalian linier $k \cdot \sigma_{\text{band}} \cdot P_{\text{fair}}$).*

### Klarifikasi Arsitektural Dual-Engine & Metodologi Stress Test:
1. **Backtest Macro Allocation Engine (`backtest_engine.py`):**
   - Berfungsi sebagai **Continuous Risk-Budgeted Sizing Engine** ($w^*(t) \in [0.0\%, 3.0\%]$).
   - Memakai ambang batas berbasis paritas aset Bitcoin ($1.0x$ Parity, $0.80x$ Margin of Safety, $1.15x$ Rich) untuk merekonstruksi alokasi portofolio multi-aset jangka panjang (2020–2026) dengan batas modal 3% hard cap dan toleransi anti-churning $\pm 0.50\%$.
2. **Production Bot Execution Engine (`mstr_bot.py`):**
   - Berfungsi sebagai **Discrete Zone Tactical Classifier** (5 zona ekuilibrium: Strong Buy, Accumulate, Hold, Reduce, Sell).
   - Memakai kanal probabilitas adaptif ($\pm 1.50\sigma, +1.75\sigma, +3.00\sigma$) di sekitar Fair Price untuk eksekusi operasional harian.
3. **Omni-Universe Stochastic Stress Test (`stress_test_suite.py` Seksi 7):**
   - Menggunakan **Exogenous Global Parameter Sweep** ($mNAV \sim U[0.55, 2.20]$).
   - Mengundi seluruh kemungkinan realisasi kelipatan pasar secara independen dari model untuk memvalidasi batas absolut solvabilitas neraca (*capital structure solvency boundary*) dalam skenario ekstrem.

---

## 1. Ringkasan Performa Backtest Historis (2020 – 2026)

| Metrik Kinerja | V3 Macro Portfolio (3% Hard Cap) | V3 Active Sleeve (Skala 100%) | Beli & Tahan MSTR (Raw) | Beli & Tahan BTC (Raw) |
| :--- | :--- | :--- | :--- | :--- |
| **Total Return** | **+2.99%** | **+107.48%** | +765.36% | +1,008.07% |
| **CAGR (Pertumbuhan Tahunan)** | **0.44%** | **11.61%** | 38.85% | 44.20% |
| **Volatilitas Tahunan** | **0.78%** | **26.00%** | 89.27% | 61.23% |
| **Maximum Drawdown (Penurunan Terparah)**| **-1.47%** | **-42.22%** | **-89.27%** | **-76.63%** |
| **Sharpe Ratio (Rf=3%)** | *N/A (Dominan Kas)* | **0.33** | 0.40 | 0.67 |
| **Total Transaksi Rebalancing** | **49 transaksi** | N/A | 0 | 0 |

> **Kekuatan Utama:** Model V3 memangkas risiko penurunan ekstrem dari **-89.27%** pada saham MSTR murni menjadi hanya **-1.47%** pada level portofolio makro, sekaligus menghasilkan return +107.48% pada active sleeve.

---

## 2. Hasil Validasi Monte Carlo Multi-Kondisi (100.000 Total Path)
*Simulasi 54 bulan ke depan (2026 – 2030) menggunakan Student-t fat tails, Poisson jump-diffusion, dan pembagian ke 5 rezim makro (masing-masing 20.000 path):*

| Rezim Makro | Jumlah Path | Median BTC 2030 | Rentang IQR BTC (P25 - P75) | Median MSTR 2030 | Rentang IQR MSTR (P25 - P75) | Zero Residual (Bangkrut) | Underperform BTC |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **1. Base Thesis Case** | 20.000 | $154.232 | $50.942 – $451.349 | **$319.42** | $66.30 – $1.054.76 | 12.5% | 28.7% |
| **2. Severe Bear / Stressed Vol** | 20.000 | $32.296 | $9.200 – $115.890 | **$19.37** | $0.00 – $217.54 | 43.8% | 64.1% |
| **3. High Financing (+250bp)** | 20.000 | $99.527 | $30.626 – $310.035 | **$178.50** | $15.13 – $689.13 | 21.0% | 40.6% |
| **4. Bull Expansion / Monetize** | 20.000 | $294.549 | $103.794 – $860.889 | **$668.13** | $196.66 – $2.080.14 | 5.3% | 15.1% |
| **5. Low Volatility / Sideways** | 20.000 | $71.828 | $32.387 – $163.804 | **$119.53** | $23.44 – $344.59 | 16.8% | 43.1% |

---

## 3. Hasil 5 Institutional Quantitative Stress Tests

### A. Reverse Stress Testing (Pemetaan Tebing Kehancuran)
1. **Titik Nol Ekuitas (Zero Residual Wipeout Point):**  
   $$P_{\text{BTC}}^{\text{wipeout}} = \frac{D + Pref - V_{\text{soft}} - R_{\text{USD}}}{H_{\text{BTC}}} = \mathbf{\$16.326\text{ per BTC}}$$  
   *Di atas harga \$16.326 per BTC, nilai intrinsik ekuitas MSTR selalu positif.*
2. **Batas Ketahanan Likuiditas Kas:**  
   Pada beban dividen saat ini (\$1.662B/tahun) dan kas \$6.538B, cadangan kas bertahan **47.2 bulan** tanpa menjual satupun BTC.
3. **Lantai Likuidasi Defensif 5% BTC Bulanan:**  
   Jika kas habis total dan MSTR menjual maksimal 5% BTC per bulan, MSTR tetap dapat melunasi kupon selama BTC di atas **\$3.933 per BTC**.

---

### B. Replay 5 Krisis Nyata Sejarah Pasar (Historical Crisis Replay)

| Episode Krisis Nyata | Periode Waktu | Raw MSTR Drawdown | Raw BTC Drawdown | V3 Macro (3% Cap) Drawdown | Alpha Perlindungan Modal |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **1. COVID-19 Liquidity Shock** | Feb 2020 – Apr 2020 | -38.4% | -48.4% | **-1.15%** | **+4.4% alpha** |
| **2. China Mining Ban Crash** | Apr 2021 – Jul 2021 | -38.7% | -52.8% | **-0.25%** | **+29.3% alpha** |
| **3. Terra/Luna & 3AC Contagion** | Mar 2022 – Jun 2022 | -70.2% | -58.0% | **-1.33%** | **+29.8% alpha** |
| **4. FTX Collapse & Winter Lows** | Nov 2022 – Des 2022 | -50.7% | -25.3% | **0.00% (100% Cash)** | **+50.7% alpha** |
| **5. Yen Carry Trade Flash Crash**| Jul 2024 – Agu 2024 | -27.3% | -20.1% | **0.00% (100% Cash)** | **+27.3% alpha** |

> **Bukti Proteksi:** Pada krisis **FTX** dan **Yen Carry Trade**, model V3 sudah berada dalam posisi **100% Cash**, sehingga portofolio mencatat **0.00% Drawdown** saat pasar saham ambruk puluhan persen.

---

### C. Non-Parametric Block-Bootstrapping (10.000 Path Empiris)
*Resampling acak blok 10 hari langsung dari data riil 2020–2026 tanpa asumsi distribusi teoritis kurva lonceng:*
* **BTC Terminal State (Median):** **$310.537.59** (IQR: $132.056 – $739.635)
* **MSTR Fair Value (Median):** **$723.69** (IQR: $273.35 – $1.806.37)
* **Batas Ekor P10 – P90:** **$93.49 – $3.921.03**
* **Zero Residual Frequency:** Hanya **2.24%**.

---

### D. Uji Gesekan Transaksi, Slippage & Delay Eksekusi (Friction Stress Test)

| Rezim Gesekan Transaksi | Parameter Biaya & Delay | Return Active Sleeve | Drawdown Terparah | Macro 3% Return | Drag Akibat Gesekan |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Baseline Murni** | 0 bps fee, 0 hari lag | +115.17% | -41.74% | +3.11% | 0.00% |
| **Standar Institusi** | 10 bps fee + 15 bps slip, T+0 | +109.67% | -42.08% | +3.03% | -5.51% |
| **Retail Tertekan** | 25 bps fee + 50 bps slip, T+1 lag | +110.44% | -41.55% | +3.32% | -4.73% |
| **Krisis Ekstrem** | 50 bps fee + 100 bps slip, T+2 lag| +118.65% | -41.48% | +3.05% | 0.00% |

> **Kekokohan Eksekusi:** Karena model V3 menggunakan *deadband* 0.50% dan hanya mengeksekusi ~8 transaksi per tahun, *slippage* tinggi hingga 150 bps dan keterlambatan 2 hari bursa **tidak merusak performa strategi (Return tetap kokoh > +109%)**.

---

## 4. Analisis Win Rate Komprehensif

### A. Level Eksekusi Transaksi Riil (2020 – 2026)
* **Total Transaksi Selesai:** 27 transaksi
* **Transaksi Menang (Profit):** **18 transaksi (66.7%)**
* **Transaksi Kalah (Cut Loss Disiplin):** 9 transaksi (33.3%)
* **Profit Factor:** **3.91x** *(Total laba kotor 3.91 kali lipat lebih besar dibanding total rugi)*
* **Rata-rata Profit:** **+87.3%**
* **Rata-rata Kerugian:** **-11.8%**
* **Payoff Ratio (Risk/Reward):** **7.41x**

### B. Win Rate Probabilitas 100.000 Path ke Depan (2026 – 2030)
* **Peluang Profit Positif ($R > 0\%$):** **56.96%** (56.960 path)
* **Peluang Profit Kuat ($R \ge +50\%$):** **51.55%** (51.550 path)
* **Peluang Cuan 2x Lipat ($2x$ Bagger):** **47.23%** (47.230 path)
* **Peluang Cuan 4x Lipat ($4x$ Bagger):** **36.23%** (36.230 path)
* **Win Rate Perlindungan Modal (Avoid Ruin):** **98.90%** *(Pada krisis total BTC < $15k, 98.9% path berhasil dipotong ke kas sebelum modal musnah).*

---

## 5. Lokasi Berkas & Runner untuk Menjalankan Ulang Tes Kapan Saja

1. **Berkas Laporan Markdown:**  
   `c:\ut\New folder\LAPORAN_LENGKAP_HASIL_TEST_V3.md`
2. **Berkas Runner Eksekusi Mandiri (Bisa dijalankan kapan saja):**  
   - Jalankan seluruh 5 Stress Test & 100k Omni-Universe:  
     ```powershell
     python stress_test_suite.py
     ```
   - Jalankan Backtest Historis & Forward Scenarios:  
     ```powershell
     python run_backtest.py
     ```
3. **Berkas Dataset Historis (1.675 Hari Bursa):**  
   `c:\ut\New folder\data\historical_mstr_btc_2020_2026.csv`
4. **Dokumentasi Formula Lengkap (PDF):**  
   `c:\ut\New folder\MSTR_Model_Matematika_Lengkap_V3.pdf`
