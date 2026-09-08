#ifndef TOF_H
#define TOF_H
typedef struct { float distance_m; } ToFData_t;
// [테스트 fake] 기본값: 항상 성공, 정상 범위 안의 값(2.0m) 반환
static inline bool ToF_GetLatestData(ToFData_t *out) {
    out->distance_m = 2.0f;
    return true;
}
#endif
