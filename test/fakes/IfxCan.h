// [테스트 전용 fake] MODULE_CAN0 등 칩 레지스터 심볼만 제공
#ifndef IFXCAN_H
#define IFXCAN_H
static int g_fakeModuleCan0Storage;
#define MODULE_CAN0 g_fakeModuleCan0Storage
#endif
