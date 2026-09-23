from datetime import datetime, timedelta, timezone
import os
import time
import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from tqdm import tqdm
from webdriver_manager.chrome import ChromeDriverManager

# 1. 깃허브 시크릿에서 민감 정보 불러오기
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
SOLAR_ID = os.environ.get("SOLAR_ID")
SOLAR_PW = os.environ.get("SOLAR_PW")

# 콤마(,)로 구분된 여러 명의 Chat ID를 리스트로 파싱 (예: "12345,67890")
TELEGRAM_CHAT_ID_RAW = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_CHAT_IDS = [
    cid.strip() for cid in TELEGRAM_CHAT_ID_RAW.split(",") if cid.strip()
]

# ==================== [사용자 설정 영역] ====================
UNIT_PRICE = 150.0  # 1kWh당 예상 단가 (원 - SMP + REC 평균 단가 등 반영)
# ==========================================================


def send_telegram_message(message):
    """등록된 모든 수신인(Chat ID)에게 텔레그램 메시지 동시 전송"""
    url_base = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    for chat_id in TELEGRAM_CHAT_IDS:
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
        try:
            response = requests.post(url_base, json=payload)
            if response.status_code == 200:
                print(f"📲 텔레그램 전송 완료 (수신인: {chat_id})")
            else:
                print(f"❌ 텔레그램 전송 실패 ({chat_id}): {response.text}")
        except Exception as e:
            print(f"❌ 텔레그램 API 통신 에러 ({chat_id}): {e}")


def monitor_solar_status(user_id, user_pw):
    print("🚀 깃허브 서버 환경(헤드리스)에서 발전소 통합 순찰 모니터링 중...")

    # 한국 시간(KST, UTC+9) 계산
    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(kst)
    current_hour = now_kst.hour
    current_minute = now_kst.minute
    current_time_str = now_kst.strftime("%Y-%m-%d %H:%M:%S")

    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    driver = None

    try:
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()), options=options
        )
        driver.get("https://solar.mrt.co.kr/")
        wait = WebDriverWait(driver, 15)

        # 1. 로그인
        print("🔑 로그인을 진행하고 있습니다...")
        for _ in tqdm(range(2), desc="로그인 준비 중"):
            time.sleep(0.3)

        id_input = wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "input[type='text'], input[placeholder*='아이디']")
            )
        )
        id_input.clear()
        id_input.send_keys(user_id)

        pw_input = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
        pw_input.clear()
        pw_input.send_keys(user_pw)

        driver.find_element(
            By.CSS_SELECTOR, "button[type='submit'], .login-btn, button"
        ).click()

        print("⏳ 대시보드 데이터 로딩 대기 중...")
        for _ in tqdm(range(8), desc="데이터 렌더링 대기 중"):
            time.sleep(1)

        total_issues = 0
        detected_source = ""

        # 2-1. 전체 '경보' 뱃지 체크
        try:
            alarm_badge = driver.find_element(
                By.CSS_SELECTOR, "span.Header__alarm-count___13qAd"
            )
            alarm_text = alarm_badge.text.strip()
            if alarm_text.isdigit():
                val = int(alarm_text)
                if val > 0:
                    total_issues += val
                    detected_source += f"경보 {val}건 "
        except Exception:
            pass

        # 2-2. 전체 '오류' 뱃지 체크
        try:
            error_div = driver.find_element(By.CSS_SELECTOR, "div.warning span")
            error_text = error_div.text.strip()
            if "오류(" in error_text:
                start_idx = error_text.find("(") + 1
                end_idx = error_text.find(")")
                if start_idx > 0 and end_idx > start_idx:
                    num_str = error_text[start_idx:end_idx]
                    if num_str.isdigit():
                        val = int(num_str)
                        if val > 0:
                            total_issues += val
                            detected_source += f"오류 {val}건 "
        except Exception:
            pass

        # ==================== [발전소 데이터 자동 크롤링] ====================
        plants_summary = []
        total_current_power = 0.0
        total_estimated_revenue = 0.0
        total_estimated_generation = 0.0

        try:
            plant_name_elements = driver.find_elements(By.CSS_SELECTOR, "div.plant-name")
            capacity_elements = driver.find_elements(By.CSS_SELECTOR, "span.capa")

            print(f"📊 화면에서 감지된 발전소 수: {len(plant_name_elements)}개소")

            for i in range(len(plant_name_elements)):
                p_name = plant_name_elements[i].text.strip()

                # 개별 발전소 카드 영역 특정
                plant_card = None
                try:
                    plant_card = plant_name_elements[i].find_element(
                        By.XPATH, "./ancestor::*[contains(@class, 'plant') or contains(@class, 'box') or contains(@class, 'card')][1]"
                    )
                except Exception:
                    try:
                        plant_card = plant_name_elements[i].find_element(By.XPATH, "./parent::*")
                    except Exception:
                        plant_card = driver

                # 1) 시설 용량 (kW) 파싱
                p_capacity = 0.0
                if i < len(capacity_elements):
                    try:
                        raw_capa = capacity_elements[i].text.replace(",", "").strip()
                        p_capacity = float(raw_capa)
                    except Exception:
                        p_capacity = 0.0

                # 2) 현재 발전량 (kW) 파싱
                p_power = 0.0
                if plant_card:
                    try:
                        power_elem = plant_card.find_element(
                            By.CSS_SELECTOR, "div.now-power, div.MediumGridBox__list-label-val___3n6GW.now-power"
                        )
                        raw_power = (
                            power_elem.text.lower()
                            .replace("kw", "")
                            .replace("kwh", "")
                            .replace(",", "")
                            .strip()
                        )
                        p_power = float(raw_power)
                    except Exception:
                        try:
                            all_powers = driver.find_elements(By.CSS_SELECTOR, "div.now-power")
                            if i < len(all_powers):
                                raw_power = (
                                    all_powers[i].text.lower()
                                    .replace("kw", "")
                                    .replace("kwh", "")
                                    .replace(",", "")
                                    .strip()
                                )
                                p_power = float(raw_power)
                        except Exception:
                            p_power = 0.0

                total_current_power += p_power

                # 3) 금일 발전시간 (h) 파싱 (사용자가 제공한 인덱스 기반 고유 XPath 직접 활용)
                p_hours = 0.0
                card_index = i + 1  # 1부터 시작하는 발전소 카드 순번
                
                # 값이 완전히 렌더링될 때까지 최대 3번 재시도
                for _ in range(3):
                    try:
                        exact_xpath = f'//*[@id="root"]/div/div/div/div/div[2]/div/div[{card_index}]/div[2]/div[1]/div[2]/span[1]'
                        time_val_elem = driver.find_element(By.XPATH, exact_xpath)
                        raw_hours = time_val_elem.text.replace(",", "").replace("h", "").strip()
                        if raw_hours:
                            val_f = float(raw_hours)
                            if val_f >= 0.0:
                                p_hours = val_f
                                break
                    except Exception:
                        pass
                    time.sleep(0.5)

                # 4) 예상 금일 발전량 및 매출 계산
                p_generation = p_capacity * p_hours
                p_revenue = p_generation * UNIT_PRICE
                
                total_estimated_generation += p_generation
                total_estimated_revenue += p_revenue

                plants_summary.append({
                    "name": p_name,
                    "power": p_power,
                    "capacity": p_capacity,
                    "hours": p_hours,
                    "generation": p_generation,
                    "revenue": p_revenue,
                })

        except Exception as e:
            print(f"⚠️ 다중 발전소 크롤링 중 예외 발생: {e}")
        # ======================================================================

        # ==================== [사업자별 집계 로직] ====================
        # 1. 한빛산업 (한빛에너지 태양광발전소)
        hanbit_capacity = 0.0
        hanbit_revenue = 0.0

        for p in plants_summary:
            if "한빛" in p["name"]:
                hanbit_capacity += p["capacity"]
                hanbit_revenue += p["revenue"]

        # 2. 다온에너지 (석계2 + 매곡4 용량 및 개별 발전시간 반영)
        sge_2_capa = 89.66
        mg_4_capa = 174.15
        daon_total_capa = sge_2_capa + mg_4_capa  # 263.81 kW

        sge_hours = 0.0
        mg_hours = 0.0
        for p in plants_summary:
            if "석계1,2" in p["name"]:
                sge_hours = p["hours"]
            elif "매곡1,2,3,4" in p["name"]:
                mg_hours = p["hours"]

        daon_generation = (sge_2_capa * sge_hours) + (mg_4_capa * mg_hours)
        daon_revenue = daon_generation * UNIT_PRICE

        # 3. 한영앤코 (나머지 모두)
        total_1_to_7_capacity = sum(p["capacity"] for p in plants_summary)
        
        hanyoung_capacity = total_1_to_7_capacity - hanbit_capacity
        hanyoung_revenue = total_estimated_revenue - hanbit_revenue - daon_revenue
        # ==============================================================

        print(
            f"🔍 [진단 완료] 총 발전소: {len(plants_summary)}개소 | 전체 현재 발전량:"
            f" {total_current_power:.2f}kW"
        )

        # 3. 알림 메시지 구성
        plant_list_text = ""
        for idx, p in enumerate(plants_summary, 1):
            plant_list_text += (
                f"{idx}. *{p['name']}*\n"
                f"    • 용량: `{p['capacity']:,.2f} kW`\n"
                f"    • 현재: `{p['power']:,.2f} kW`\n"
                f"    • 발전시간: `{p['hours']:,.2f} h`\n"
                f"    • 예상매출: `{p['revenue']:,.0f} 원`\n\n"
            )

        owner_summary_text = (
            "🏢 *[사업자별 현황 요약]*\n\n"
            "1. *한빛산업*\n"
            f"    • 용량: `{hanbit_capacity:,.2f} kW`\n"
            f"    • 예상매출: `{hanbit_revenue:,.0f} 원`\n\n"
            "2. *한영앤코*\n"
            f"    • 용량: `{hanyoung_capacity:,.2f} kW`\n"
            f"    • 예상매출: `{hanyoung_revenue:,.0f} 원`\n\n"
            "3. *다온에너지*\n"
            f"    • 용량: `{daon_total_capa:,.2f} kW`\n"
            f"    • 예상매출: `{daon_revenue:,.0f} 원`\n\n"
        )

        # 3-1. 이상이 감지된 경우 즉시 긴급 경고 발송
        if total_issues > 0:
            alert_msg = (
                "🚨 *[태양광 발전소 긴급 이상 경고]*\n\n"
                f"• 감지 상태: *{detected_source.strip()}* 발생!\n"
                "• 이상이 생긴 발전소를 즉시 확인해 주세요.\n\n"
                "⚡ *[발전소별 실시간 현황]*\n"
                f"{plant_list_text}"
                f"• 단가: `{int(UNIT_PRICE)}원/kW` (보수적 기준)\n"
                f"• 합계 현재 발전량: `{total_current_power:,.2f} kW`\n"
                f"• 전체 일일 예상 매출액: `{total_estimated_revenue:,.0f} 원`\n\n"
                f"{owner_summary_text}"
                f"• 확인 시간: {current_time_str}"
            )
            send_telegram_message(alert_msg)
            print("🚨 문제가 감지되어 텔레그램으로 즉시 긴급 알림을 전송했습니다.")

        # 3-2. 정기 리포트 시간(12시, 15시, 18시) 정각 30분 미만 실행 시 발송
        if current_hour in [12, 15, 18] and current_minute < 30:
            heartbeat_msg = (
                "🟢 *[태양광 봇 정기 가동 리포트]*\n\n"
                "• 상태: 정상 가동 중 🛡️\n"
                "• 현재 시간대 순찰 점검이 완료되었습니다.\n\n"
                "📊 *[발전소별 실시간 현황]*\n"
                f"{plant_list_text}"
                f"• 단가: `{int(UNIT_PRICE)}원/kW` (보수적 기준)\n"
                f"• 합계 현재 발전량: `{total_current_power:,.2f} kW`\n"
                f"• 전체 일일 예상 매출액: `{total_estimated_revenue:,.0f} 원`\n\n"
                f"{owner_summary_text}"
                f"• 확인 시간: {current_time_str}"
            )
            send_telegram_message(heartbeat_msg)
            print(
                f"🟢 정기 리포트 시간대({current_hour}시 정각 턴) 도래: 통합 리포트를 전송했습니다."
            )
        else:
            print("🟢 정기 리포트 조건에 해당하지 않음.")

        if total_issues == 0 and not (
            current_hour in [12, 15, 18] and current_minute < 30
        ):
            print("🟢 이상 없음. 추가 알림은 생략합니다.")

    except Exception as e:
        clean_error = str(e).split("\n")[0]
        error_msg = f"❌ *[모니터링 스크립트 실행 오류]*\n`{clean_error}`"
        send_telegram_message(error_msg)
        print(f"❌ 오류 상세 내용: {e}")

    finally:
        for _ in tqdm(range(2), desc="브라우저 세션 정리 중"):
            time.sleep(0.5)
        if driver:
            driver.quit()
        print("🔒 깃허브 서버 세션이 안전하게 종료되었습니다.")


if __name__ == "__main__":
    if not SOLAR_ID or not SOLAR_PW or not TELEGRAM_TOKEN or not TELEGRAM_CHAT_IDS:
        print(
            "❌ 오류: 깃허브 시크릿 환경변수가 설정되지 않았습니다. (SOLAR_ID, SOLAR_PW, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID 확인 필요)"
        )
    else:
        monitor_solar_status(SOLAR_ID, SOLAR_PW)
