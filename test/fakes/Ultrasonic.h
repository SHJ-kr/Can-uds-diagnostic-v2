#ifndef ULTRASONIC_H
#define ULTRASONIC_H
typedef enum { US_LEFT = 0, US_RIGHT = 1, US_REAR = 2 } UltrasonicSide;
typedef struct { unsigned int dist_raw_mm; } UltrasonicData_t;
// [테스트 fake] 기본값: 항상 성공, 정상 범위 안의 값(200mm) 반환
static inline bool Ultrasonic_GetLatestData(UltrasonicSide side, UltrasonicData_t *out) {
    (void)side;
    out->dist_raw_mm = 200;
    return true;
}
#endif
