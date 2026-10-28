# RESULTS (generado por scripts/report.py)

**Mejor evaluación:** `20261028_031252_heuristic_level1_gate1000` — policy `heuristic_scripted` success 1.000 (n=1000), colisiones 0.000, drops 0.000

**Último checkpoint:** `act_lowdim_smooth_v001` — dataset `level1_cube_lowdim_smooth_v001` (200 ep, 18580 frames), step 3000, loss final 0.10976057536900044

| run | policy | n | success | grasp | collision | drop | timeout | ciclo med [s] | p95 [s] | latencia [ms] | gate | fallos | git |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 20261028_024023_heuristic_smoke | heuristic_scripted | 50 | 0.000 | 0.000 | 0.000 | 0.000 | 1.000 | nan | nan | 0.1 | False | {'TIMEOUT': 50} | af9f850 |
| 20261028_024106_heuristic_smoke2 | heuristic_scripted | 50 | 0.000 | 1.000 | 0.000 | 0.000 | 1.000 | nan | nan | 0.0 | False | {'TIMEOUT': 50} | af9f850 |
| 20261028_024236_heuristic_smoke3 | heuristic_scripted | 100 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 6.70 | 6.70 | 0.0 | True | {'NONE': 100} | af9f850 |
| 20261028_025605_heuristic_level1_rand | heuristic_scripted | 200 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 6.80 | 7.00 | 0.0 | True | {'NONE': 200} | f363090 |
| 20261028_031252_heuristic_level1_gate1000 | heuristic_scripted | 1000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 6.80 | 7.00 | 0.0 | True | {'NONE': 1000} | fc8c85c |
| 20261028_031648_perception_perception_smoke | perception_scripted | 20 | 0.000 | 0.000 | 0.000 | 0.000 | 1.000 | nan | nan | 0.2 | False | {'TIMEOUT': 20} | fc8c85c |
| 20261028_032307_perception_perception_smoke2 | perception_scripted | 20 | 0.000 | 0.000 | 0.000 | 0.000 | 1.000 | nan | nan | 0.1 | False | {'TIMEOUT': 20} | fc8c85c |
| 20261028_032402_perception_perception_smoke3 | perception_scripted | 30 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 6.80 | 6.95 | 0.1 | True | {'NONE': 30} | fc8c85c |
| 20261028_032427_perception_perception_wide_rand | perception_scripted | 200 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 6.90 | 7.40 | 0.1 | True | {'NONE': 200} | fc8c85c |
| 20261028_032724_perception_level2_perception | perception_scripted | 100 | 0.300 | 0.530 | 0.070 | 0.220 | 0.010 | 6.65 | 7.15 | 0.2 | False | {'NO_GRASP': 18, 'NONE': 30, 'COLLISION': 7, 'SLIP': 26, 'DROP': 18, 'TIMEOUT': 1} | 823f387 |
| 20261028_033431_perception_level2_perception_v2 | perception_scripted | 100 | 0.830 | 0.990 | 0.000 | 0.000 | 0.160 | 8.10 | 13.24 | 0.1 | False | {'NONE': 83, 'TIMEOUT': 16, 'SLIP': 1} | 823f387 |
| 20261028_033825_perception_level1_perception_v3 | perception_scripted | 200 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 8.10 | 8.40 | 0.2 | True | {'NONE': 200} | 823f387 |
| 20261028_034116_perception_level2_perception_v3 | perception_scripted | 100 | 0.840 | 0.840 | 0.000 | 0.000 | 0.000 | 8.00 | 8.70 | 0.2 | False | {'NONE': 84, 'SLIP': 16} | 823f387 |
| 20261028_034354_heuristic_level2_privileged | heuristic_scripted | 100 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 8.00 | 8.70 | 0.1 | True | {'NONE': 100} | 823f387 |
| 20261028_034710_perception_level2_perception_v4 | perception_scripted | 100 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 8.00 | 8.60 | 0.2 | True | {'NONE': 100} | 823f387 |
| 20261028_035020_perception_level1_perception_gate1000 | perception_scripted | 1000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 8.10 | 8.40 | 0.2 | True | {'NONE': 1000} | c6b3cb8 |
| 20261028_035133_perception_level3_perception_v1 | perception_scripted | 120 | 0.892 | 0.933 | 0.025 | 0.000 | 0.033 | 8.00 | 8.70 | 0.2 | False | {'NONE': 107, 'PLACE_FAIL': 5, 'TIMEOUT': 4, 'COLLISION': 3, 'SLIP': 1} | 5143c5d |
| 20261028_035524_heuristic_level3_privileged | heuristic_scripted | 120 | 0.925 | 0.983 | 0.000 | 0.000 | 0.017 | 8.00 | 8.70 | 0.1 | False | {'NONE': 111, 'PLACE_FAIL': 7, 'TIMEOUT': 2} | 5143c5d |
| 20261028_035828_perception_level3_perception_v2 | perception_scripted | 120 | 0.908 | 0.933 | 0.017 | 0.000 | 0.000 | 7.80 | 8.50 | 0.2 | False | {'NONE': 109, 'BAD_TARGET': 5, 'PLACE_FAIL': 3, 'COLLISION': 2, 'SLIP': 1} | 5143c5d |
| 20261028_040405_perception_level2_perception_gate1000 | perception_scripted | 1000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | 7.90 | 8.50 | 0.2 | True | {'NONE': 1000} | 9eafd4c |
| 20261028_042505_act_act_step500 | act (act_level1_v001_step500) | 30 | 0.533 | 0.533 | 0.000 | 0.000 | 1.000 | 15.00 | 15.00 | 125.7 | False | {'TIMEOUT': 14, 'NONE': 16} | fa56668 |
| 20261028_044826_act_act_step1000 | act (act_level1_v001_step1000) | 30 | 0.500 | 0.600 | 0.000 | 0.100 | 0.500 | 6.50 | 6.60 | 121.8 | False | {'TIMEOUT': 12, 'NONE': 15, 'DROP': 3} | 3f2e2ff |
| 20261028_050302_act_act_step1000_nas5 | act (act_level1_v001_step1000) | 30 | 0.100 | 0.467 | 0.000 | 0.367 | 0.900 | 6.50 | 6.50 | 258.9 | False | {'TIMEOUT': 16, 'NONE': 3, 'DROP': 11} | 2f3e4d3 |
| 20261028_075425_act_act_step2000 | act (act_level1_v001_step2000) | 30 | 0.533 | 0.533 | 0.000 | 0.000 | 0.467 | 6.00 | 6.15 | 130.0 | False | {'TIMEOUT': 14, 'NONE': 16} | 51eed54 |
| 20261028_083727_act_act_crop_step1000_full | act (act_level1_crop_v001_step1000) | 30 | 0.567 | 0.600 | 0.067 | 0.000 | 0.367 | 7.30 | 7.60 | 6.0 | False | {'COLLISION': 2, 'TIMEOUT': 11, 'NONE': 17} | 444140c |
| 20261028_084303_act_act_crop_step2000_full | act (act_level1_crop_v001_step2000) | 30 | 0.500 | 0.567 | 0.100 | 0.000 | 0.333 | 7.30 | 7.97 | 5.8 | False | {'COLLISION': 3, 'NONE': 15, 'PLACE_FAIL': 2, 'TIMEOUT': 10} | 444140c |
| 20261028_092256_act_act_h6_step2000 | act (act_level1_crop_noshift_v001_step2000) | 30 | 0.567 | 0.567 | 0.167 | 0.000 | 0.267 | 7.10 | 7.60 | 6.2 | False | {'TIMEOUT': 8, 'NONE': 17, 'COLLISION': 5} | a2c1a63 |
| 20261028_105859_act_act_lowdim_v001 | act (act_lowdim_level1_v001) | 30 | 0.000 | 0.000 | 0.000 | 0.000 | 1.000 | nan | nan | 1.5 | False | {'TIMEOUT': 30} | 4f4fb56 |
| 20261028_113033_act_act_lowdim_v002 | act (act_lowdim_level1_v002) | 30 | 0.000 | 0.200 | 0.033 | 0.200 | 0.967 | nan | nan | 3.2 | False | {'TIMEOUT': 23, 'DROP': 6, 'COLLISION': 1} | 7962203 |
| 20261028_120441_act_act_lowdim_v003 | act (act_lowdim_level1_v003) | 30 | 0.033 | 0.567 | 0.033 | 0.567 | 0.933 | 7.70 | 7.70 | 1.7 | False | {'TIMEOUT': 12, 'DROP': 16, 'COLLISION': 1, 'NONE': 1} | 42f9601 |
| 20261028_124619_act_act_lowdim_dart_v001 | act (act_lowdim_dart_v001) | 30 | 0.033 | 0.500 | 0.000 | 0.400 | 0.933 | 5.90 | 5.90 | 1.3 | False | {'TIMEOUT': 16, 'DROP': 12, 'PLACE_FAIL': 1, 'NONE': 1} | f9c8d4b |
| 20261028_124752_act_act_lowdim_level1_v003_nas1 | act (act_lowdim_level1_v003) | 30 | 0.000 | 0.067 | 0.000 | 0.033 | 1.000 | nan | nan | 4.1 | False | {'TIMEOUT': 29, 'DROP': 1} | bf33e50 |
| 20261028_124839_act_act_lowdim_dart_v001_nas1 | act (act_lowdim_dart_v001) | 30 | 0.000 | 0.000 | 0.000 | 0.000 | 1.000 | nan | nan | 4.3 | False | {'TIMEOUT': 30} | bf33e50 |
| 20261028_124928_act_act_lowdim_dart_v001_nas3 | act (act_lowdim_dart_v001) | 30 | 0.000 | 0.067 | 0.000 | 0.000 | 1.000 | nan | nan | 2.1 | False | {'TIMEOUT': 30} | bf33e50 |
| 20261028_131958_act_act_lowdim_smooth_nas10 | act (act_lowdim_smooth_v001) | 30 | 0.067 | 1.000 | 0.000 | 0.833 | 0.800 | 8.80 | 8.80 | 1.3 | False | {'PLACE_FAIL': 3, 'DROP': 25, 'NONE': 2} | 9c1b143 |
| 20261028_132035_act_act_lowdim_smooth_nas5 | act (act_lowdim_smooth_v001) | 30 | 0.833 | 1.000 | 0.000 | 0.100 | 0.167 | 12.90 | 14.78 | 1.6 | False | {'NONE': 25, 'TIMEOUT': 2, 'DROP': 3} | 9c1b143 |
| 20261028_132206_act_act_lowdim_smooth_nas3 | act (act_lowdim_smooth_v001) | 30 | 0.100 | 0.400 | 0.000 | 0.267 | 0.900 | 11.20 | 13.09 | 3.0 | False | {'TIMEOUT': 19, 'NONE': 3, 'DROP': 8} | 9c4fac8 |
| 20261028_132341_act_act_lowdim_smooth_nas4 | act (act_lowdim_smooth_v001) | 30 | 0.600 | 1.000 | 0.000 | 0.300 | 0.400 | 12.15 | 14.41 | 2.0 | False | {'NONE': 18, 'DROP': 9, 'TIMEOUT': 3} | 9c4fac8 |
| 20261028_132420_act_act_lowdim_smooth_nas5_100 | act (act_lowdim_smooth_v001) | 100 | 0.770 | 1.000 | 0.000 | 0.190 | 0.240 | 12.90 | 14.40 | 1.7 | False | {'NONE': 77, 'DROP': 19, 'TIMEOUT': 4} | 9c4fac8 |
| 20261028_135550_act_act_lowdim_floor_nas5 | act (act_lowdim_smooth_floor_v001) | 30 | 0.900 | 1.000 | 0.000 | 0.000 | 0.200 | 13.80 | 15.00 | 1.8 | False | {'NONE': 27, 'TIMEOUT': 3} | fe855d8 |
| 20261028_135630_act_act_lowdim_floor_nas3 | act (act_lowdim_smooth_floor_v001) | 30 | 0.533 | 0.700 | 0.000 | 0.167 | 0.467 | 13.30 | 14.48 | 2.8 | False | {'TIMEOUT': 9, 'DROP': 5, 'NONE': 16} | fe855d8 |
| 20261028_135850_act_act_lowdim_floor_nas6 | act (act_lowdim_smooth_floor_v001) | 30 | 0.200 | 1.000 | 0.000 | 0.767 | 0.800 | 11.65 | 13.60 | 1.6 | False | {'TIMEOUT': 1, 'DROP': 23, 'NONE': 6} | 39058fc |
| 20261028_135929_act_act_lowdim_floor_nas8 | act (act_lowdim_smooth_floor_v001) | 30 | 0.000 | 1.000 | 0.000 | 1.000 | 1.000 | nan | nan | 1.4 | False | {'DROP': 30} | 39058fc |
| 20261028_140009_act_act_lowdim_floor_nas5_100 | act (act_lowdim_smooth_floor_v001) | 100 | 0.840 | 1.000 | 0.000 | 0.030 | 0.180 | 13.35 | 14.79 | 1.6 | False | {'NONE': 84, 'TIMEOUT': 13, 'DROP': 3} | 39058fc |
| 20261028_144055_act_act_lowdim_floor_dart_nas5_100 | act (act_lowdim_floor_dart_v001) | 100 | 0.190 | 0.780 | 0.000 | 0.590 | 0.810 | 6.40 | 6.54 | 1.8 | False | {'DROP': 59, 'TIMEOUT': 22, 'NONE': 19} | 7ded230 |
| 20261028_152350_act_act_lowdim_floor400_nas5_100 | act (act_lowdim_floor400_v001) | 100 | 0.170 | 1.000 | 0.000 | 0.650 | 0.830 | 12.70 | 14.80 | 1.6 | False | {'NONE': 17, 'DROP': 65, 'TIMEOUT': 18} | 0f715f2 |
| 20261028_152716_act_act_lowdim_floor_nas5_gate1000 | act (act_lowdim_smooth_floor_v001) | 1000 | 0.872 | 0.997 | 0.000 | 0.038 | 0.162 | 13.60 | 14.80 | 1.7 | False | {'NONE': 872, 'TIMEOUT': 90, 'DROP': 38} | 5755ab3 |
