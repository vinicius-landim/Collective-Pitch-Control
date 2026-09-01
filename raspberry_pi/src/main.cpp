#include <gpiod.hpp>
#include <iostream>
#include <chrono>
#include <thread>
#include <cstdint>
#include <atomic>
#include <csignal>
#include <cstring>
#include <cstdio>
#include <fstream>
#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <climits>

#include <sys/socket.h>
#include <arpa/inet.h>
#include <unistd.h>

#define CHIP_PATH "/dev/gpiochip0"

// Rede UDP (override: export SCCA_DASHBOARD_IP=10.0.0.5)
constexpr const char* DASHBOARD_IP_DEFAULT = "10.0.0.5";
constexpr int UDP_RX_PORT = 5005;
constexpr int UDP_TX_PORT = 5006;

// HX711
constexpr int HX711_DATA  = 5;
constexpr int HX711_CLOCK = 6;
constexpr int HX_MAX_HIGH_US = 60;

// Botões
constexpr int BTN_UP           = 17;
constexpr int BTN_DOWN         = 27;
constexpr int BTN_TRIM_RELEASE = 22;

// Fins de curso
constexpr int LIMIT_TOP        = 24;
constexpr int LIMIT_BOTTOM     = 25;

// Motor
constexpr int MOTOR_PUL = 18;
constexpr int MOTOR_DIR = 23;
constexpr int MOTOR_ENA = 16;

constexpr int STEP_DELAY_US = 7000;

// Override via célula de carga (mesmos limites da interface)
constexpr int OVERRIDE_STOPPED_LIMIT = 30000;
constexpr int OVERRIDE_UP_LIMIT      = 180000;
constexpr int OVERRIDE_DOWN_LIMIT    = -180000;

// Margem mínima em relação aos limites calibrados do transdutor
constexpr int CALIBRATION_MARGIN_MIN = 500;

// IDs de manobra recebidos no pacote P
constexpr int MANEUVER_NONE              = 0;
constexpr int MANEUVER_FULL_STROKE       = 1; // 0% -> 100% -> 0%, 3 ciclos
constexpr int MANEUVER_FAST_UP_SLOW_DOWN = 2; // sobe rapido / desce devagar, 2 ciclos
constexpr int MANEUVER_OSCILLATE_15_45   = 3; // oscila entre 15% e 45%
constexpr int MANEUVER_STEP_SEQUENCE     = 4;

constexpr int PA_PHASE_EXECUTE     = 0;
constexpr int PA_PHASE_RETURN_HOME = 1;
constexpr int PA_PHASE_COMPLETE    = 2;

std::atomic<bool> running(true);

std::atomic<bool> g_up(false);
std::atomic<bool> g_down(false);
std::atomic<bool> g_trimRelease(false);
std::atomic<bool> g_limitTop(false);
std::atomic<bool> g_limitBottom(false);

std::atomic<int32_t> g_hxRaw(0);
std::atomic<int32_t> g_hxNet(0);
std::atomic<int> g_hxInvalid(0);

std::atomic<int> g_posTop(0);
std::atomic<int> g_posBottom(0);
std::atomic<bool> g_calibrated(false);

//  1 = UP
// -1 = DOWN
//  0 = parado/travado
//  2 = trim release / motor livre
std::atomic<int> g_movement(0);

std::atomic<int> g_override(0);
std::atomic<int> g_autopilotActive(0);
std::atomic<int> g_hydraulicFailure(0);
std::atomic<int> g_transducerPosition(0);
std::atomic<int> g_maneuverId(0);
std::atomic<int> g_udpCommandsReceived(0);

// Faixa observada do transdutor durante a calibragem
int g_calibTransducerMin = INT_MAX;
int g_calibTransducerMax = INT_MIN;

// Estado interno do piloto automático
int g_paPhase = PA_PHASE_EXECUTE;
int g_paHomePosition = 0;
int g_paCycleCount = 0;
double g_paTargetPercent = 100.0;
int g_paSlowStepCounter = 0;
int g_paStepIndex = 0;
std::chrono::steady_clock::time_point g_paStepHoldStart;

bool stepMotor(gpiod::line_request& request, bool direction);

const char* resolveDashboardIp()
{
    const char* fromEnv = std::getenv("SCCA_DASHBOARD_IP");
    if (fromEnv != nullptr && fromEnv[0] != '\0')
        return fromEnv;
    return DASHBOARD_IP_DEFAULT;
}

int readEnvInt(const char* name, int defaultValue)
{
    const char* fromEnv = std::getenv(name);
    if (fromEnv == nullptr || fromEnv[0] == '\0')
        return defaultValue;
    return std::atoi(fromEnv);
}

void trackTransducerReading(int& minSeen, int& maxSeen)
{
    int position = g_transducerPosition.load();
    if (position < minSeen)
        minSeen = position;
    if (position > maxSeen)
        maxSeen = position;
}

int averageTransducerReading(int samples = 8)
{
    long long sum = 0;
    for (int i = 0; i < samples; ++i)
    {
        sum += g_transducerPosition.load();
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
    return static_cast<int>(sum / samples);
}

bool waitForDashboardLink()
{
    const int timeoutSec = readEnvInt("SCCA_CALIB_UDP_TIMEOUT_SEC", 60);
    std::cout << "[CALIB] Aguardando pacotes P do dashboard (timeout "
              << timeoutSec << "s)..." << std::endl;
    std::cout << "[CALIB] Inicie o dashboard em "
              << resolveDashboardIp()
              << " ANTES de rodar o agente na Raspberry." << std::endl;

    auto start = std::chrono::steady_clock::now();
    while (running)
    {
        if (g_udpCommandsReceived.load() > 0)
        {
            std::cout << "[CALIB] Link UDP OK | pacotes P recebidos: "
                      << g_udpCommandsReceived.load()
                      << " | transdutor=" << g_transducerPosition.load()
                      << std::endl;
            return true;
        }

        auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(
            std::chrono::steady_clock::now() - start
        ).count();

        if (elapsed >= timeoutSec)
            break;

        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }

    std::cerr << "[CALIB] ERRO: Nenhum pacote P recebido do dashboard." << std::endl;
    std::cerr << "  1) Dashboard rodando? (python main.py em "
              << resolveDashboardIp() << ")" << std::endl;
    std::cerr << "  2) Ping entre as maquinas: ping "
              << resolveDashboardIp() << " (na Pi) e ping 10.0.0.1 (no dashboard)" << std::endl;
    std::cerr << "  3) IP estatico na Pi: sudo bash scripts/configure-static-ip.sh eth0"
              << std::endl;
    std::cerr << "  4) Joystick/transdutor conectado no dashboard (/dev/input/js0)"
              << std::endl;
    return false;
}

void signalHandler(int)
{
    running = false;
}

void updateOverride()
{
    int movement = g_movement.load();
    int32_t hxNet = g_hxNet.load();
    int overrideValue = 0;

    if (movement == 0)
    {
        if (std::abs(hxNet) > OVERRIDE_STOPPED_LIMIT)
            overrideValue = 1;
    }
    else if (movement == 1)
    {
        if (hxNet > OVERRIDE_UP_LIMIT)
            overrideValue = 1;
    }
    else if (movement == -1)
    {
        if (hxNet < OVERRIDE_DOWN_LIMIT)
            overrideValue = 1;
    }

    g_override = overrideValue;
}

void resetPaManeuverState()
{
    g_paHomePosition = g_transducerPosition.load();
    g_paPhase = PA_PHASE_EXECUTE;
    g_paCycleCount = 0;
    g_paSlowStepCounter = 0;
    g_paStepIndex = 0;
    g_paStepHoldStart = std::chrono::steady_clock::now();

    switch (g_maneuverId.load())
    {
        case MANEUVER_OSCILLATE_15_45:
            g_paTargetPercent = 45.0;
            break;
        default:
            g_paTargetPercent = 100.0;
            break;
    }

    g_up = false;
    g_down = false;
    g_movement = 0;
}

int calibrationMargin(int span)
{
    if (span <= 0)
        return CALIBRATION_MARGIN_MIN;
    return std::max(CALIBRATION_MARGIN_MIN, span / 20);
}

int usefulLowerBound()
{
    int lower = std::min(g_posTop.load(), g_posBottom.load());
    int upper = std::max(g_posTop.load(), g_posBottom.load());
    return lower + calibrationMargin(upper - lower);
}

int usefulUpperBound()
{
    int lower = std::min(g_posTop.load(), g_posBottom.load());
    int upper = std::max(g_posTop.load(), g_posBottom.load());
    return upper - calibrationMargin(upper - lower);
}

int usefulCenter()
{
    return (usefulLowerBound() + usefulUpperBound()) / 2;
}

int clampToUsefulArea(int position)
{
    int lower = usefulLowerBound();
    int upper = usefulUpperBound();
    return std::max(lower, std::min(upper, position));
}

bool positionWithinCalibration(int targetPosition)
{
    int lowerBound = std::min(g_posTop.load(), g_posBottom.load());
    int upperBound = std::max(g_posTop.load(), g_posBottom.load());
    return (targetPosition >= lowerBound) && (targetPosition <= upperBound);
}

int calibrationLowerBound()
{
    return std::min(g_posTop.load(), g_posBottom.load());
}

int calibrationUpperBound()
{
    return std::max(g_posTop.load(), g_posBottom.load());
}

int positionFromPercent(double percent)
{
    int lower = calibrationLowerBound();
    int upper = calibrationUpperBound();
    percent = std::max(0.0, std::min(100.0, percent));
    return lower + static_cast<int>((upper - lower) * percent / 100.0);
}

int maneuverDeadband()
{
    int span = calibrationUpperBound() - calibrationLowerBound();
    return std::max(300, span / 60);
}

bool atTargetPosition(int target, int deadband)
{
    return std::abs(g_transducerPosition.load() - target) <= deadband;
}

bool stepTowardTarget(gpiod::line_request& request, int target, int deadband)
{
    int pos = g_transducerPosition.load();
    int lower = calibrationLowerBound();
    int upper = calibrationUpperBound();

    if (pos < target - deadband)
    {
        if (pos >= upper)
            return false;
        return stepMotor(request, true);
    }

    if (pos > target + deadband)
    {
        if (pos <= lower)
            return false;
        return stepMotor(request, false);
    }

    return false;
}

bool stepMotorTowardTransducer(gpiod::line_request& request, bool increasePosition)
{
    int current = g_transducerPosition.load();

    if (increasePosition)
    {
        if (current >= usefulUpperBound())
            return false;
        return stepMotor(request, true);
    }

    if (current <= usefulLowerBound())
        return false;
    return stepMotor(request, false);
}

void setSimulatedTrimButtons(bool movingUp, bool movingDown, bool stepped)
{
    g_up = stepped && movingUp;
    g_down = stepped && movingDown;
    if (!stepped)
        g_movement = 0;
    else if (movingUp)
        g_movement = 1;
    else if (movingDown)
        g_movement = -1;
    else
        g_movement = 0;
}

bool runPercentStrokeManeuver(
    gpiod::line_request& request,
    double lowPercent,
    double highPercent,
    int maxHalfStrokes,
    int slowDownDivisor)
{
    int target = positionFromPercent(g_paTargetPercent);
    int deadband = maneuverDeadband();
    int pos = g_transducerPosition.load();

    if (atTargetPosition(target, deadband))
    {
        g_paCycleCount++;
        if (g_paCycleCount >= maxHalfStrokes)
        {
            g_paPhase = PA_PHASE_RETURN_HOME;
            g_up = false;
            g_down = false;
            g_movement = 0;
            return true;
        }

        g_paTargetPercent = (g_paTargetPercent >= highPercent - 0.5)
            ? lowPercent
            : highPercent;
        g_paSlowStepCounter = 0;
        g_up = false;
        g_down = false;
        g_movement = 0;
        return true;
    }

    bool movingUp = pos < target - deadband;
    bool shouldStep = true;
    if (!movingUp && slowDownDivisor > 1)
    {
        g_paSlowStepCounter++;
        shouldStep = (g_paSlowStepCounter % slowDownDivisor) == 0;
    }

    bool stepped = false;
    if (shouldStep)
        stepped = stepTowardTarget(request, target, deadband);

    setSimulatedTrimButtons(movingUp && stepped, !movingUp && stepped, stepped);
    return true;
}

bool runManeuverFullStroke(gpiod::line_request& request)
{
    return runPercentStrokeManeuver(request, 0.0, 100.0, 6, 1);
}

bool runManeuverFastUpSlowDown(gpiod::line_request& request)
{
    return runPercentStrokeManeuver(request, 0.0, 100.0, 4, 4);
}

bool runManeuverOscillate1545(gpiod::line_request& request)
{
    return runPercentStrokeManeuver(request, 15.0, 45.0, INT_MAX, 1);
}

bool runReturnHomePhase(gpiod::line_request& request)
{
    int home = g_paHomePosition;
    int deadband = maneuverDeadband();
    int pos = g_transducerPosition.load();

    if (atTargetPosition(home, deadband))
    {
        g_paPhase = PA_PHASE_COMPLETE;
        g_up = false;
        g_down = false;
        g_movement = 0;
        return false;
    }

    bool stepped = stepTowardTarget(request, home, deadband);
    setSimulatedTrimButtons(pos < home - deadband, pos > home + deadband, stepped);
    return true;
}

bool runManeuverStepSequence(gpiod::line_request& request)
{
    constexpr double holdSec = 2.0;
    constexpr int stepCount = 4;
    int lower = calibrationLowerBound();
    int upper = calibrationUpperBound();
    int span = upper - lower;

    const int levelTargets[4] = {
        lower + span * 20 / 100,
        lower + span * 45 / 100,
        lower + span * 75 / 100,
        lower + span * 30 / 100,
    };

    int target = levelTargets[g_paStepIndex % stepCount];
    int pos = g_transducerPosition.load();
    int deadband = maneuverDeadband();

    if (atTargetPosition(target, deadband))
    {
        auto now = std::chrono::steady_clock::now();
        double held = std::chrono::duration<double>(now - g_paStepHoldStart).count();
        if (held >= holdSec)
        {
            g_paStepIndex++;
            g_paStepHoldStart = now;

            if (g_paStepIndex >= stepCount)
            {
                g_paPhase = PA_PHASE_RETURN_HOME;
                g_up = false;
                g_down = false;
                g_movement = 0;
                return true;
            }
        }

        g_up = false;
        g_down = false;
        g_movement = 0;
        return true;
    }

    g_paStepHoldStart = std::chrono::steady_clock::now();

    bool stepped = stepTowardTarget(request, target, deadband);
    setSimulatedTrimButtons(pos < target - deadband, pos > target + deadband, stepped);
    return true;
}

bool runAutopilotManeuver(gpiod::line_request& request, bool trimHold)
{
    const bool apActive = g_autopilotActive.load();
    const int maneuver = g_maneuverId.load();

    if (g_paPhase == PA_PHASE_COMPLETE)
        return false;

    const bool finishingReturnHome = (g_paPhase == PA_PHASE_RETURN_HOME);
    if (!finishingReturnHome && (!apActive || maneuver == MANEUVER_NONE))
        return false;

    if (!trimHold || g_override.load() || !g_calibrated.load())
        return false;

    g_trimRelease = false;
    request.set_value(MOTOR_ENA, gpiod::line::value::INACTIVE);

    bool runningManeuver = true;
    if (g_paPhase == PA_PHASE_RETURN_HOME)
    {
        runningManeuver = runReturnHomePhase(request);
    }
    else
    {
        switch (g_maneuverId.load())
        {
            case MANEUVER_FULL_STROKE:
                runningManeuver = runManeuverFullStroke(request);
                break;
            case MANEUVER_FAST_UP_SLOW_DOWN:
                runningManeuver = runManeuverFastUpSlowDown(request);
                break;
            case MANEUVER_OSCILLATE_15_45:
                runningManeuver = runManeuverOscillate1545(request);
                break;
            case MANEUVER_STEP_SEQUENCE:
                runningManeuver = runManeuverStepSequence(request);
                break;
            default:
                return false;
        }
    }

    updateOverride();
    if (g_override.load())
        return false;

    return runningManeuver;
}

bool buttonPressed(gpiod::line_request& request, int gpio)
{
    return request.get_value(gpio) == gpiod::line::value::INACTIVE;
}

bool hx711Ready(gpiod::line_request& request)
{
    return request.get_value(HX711_DATA) == gpiod::line::value::INACTIVE;
}

int32_t hx711ReadRawValidated(gpiod::line_request& request, bool& valid)
{
    valid = true;
    int32_t value = 0;
    int timeout = 0;

    while (!hx711Ready(request) && running)
    {
        std::this_thread::sleep_for(std::chrono::milliseconds(1));

        if (++timeout > 1000)
        {
            valid = false;
            return 0;
        }
    }

    for (int i = 0; i < 24; i++)
    {
        auto highStart = std::chrono::steady_clock::now();

        request.set_value(HX711_CLOCK, gpiod::line::value::ACTIVE);

        value <<= 1;

        if (request.get_value(HX711_DATA) == gpiod::line::value::ACTIVE)
            value++;

        request.set_value(HX711_CLOCK, gpiod::line::value::INACTIVE);

        auto highEnd = std::chrono::steady_clock::now();

        auto highUs = std::chrono::duration_cast<std::chrono::microseconds>(
            highEnd - highStart
        ).count();

        if (highUs >= HX_MAX_HIGH_US)
            valid = false;
    }

    auto highStart = std::chrono::steady_clock::now();

    request.set_value(HX711_CLOCK, gpiod::line::value::ACTIVE);
    request.set_value(HX711_CLOCK, gpiod::line::value::INACTIVE);

    auto highEnd = std::chrono::steady_clock::now();

    auto highUs = std::chrono::duration_cast<std::chrono::microseconds>(
        highEnd - highStart
    ).count();

    if (highUs >= HX_MAX_HIGH_US)
        valid = false;

    if (value & 0x800000)
        value |= 0xFF000000;

    return value;
}

int32_t hx711ReadValidAverage(gpiod::line_request& request, int samples, int& invalidCount)
{
    int64_t sum = 0;
    int validSamples = 0;

    while (validSamples < samples && running)
    {
        bool valid = true;
        int32_t raw = hx711ReadRawValidated(request, valid);

        if (valid)
        {
            sum += raw;
            validSamples++;
        }
        else
        {
            invalidCount++;
        }
    }

    if (validSamples == 0)
        return 0;

    return static_cast<int32_t>(sum / validSamples);
}

void hx711Thread()
{
    auto chip = gpiod::chip(CHIP_PATH);

    auto inputSettings = gpiod::line_settings()
        .set_direction(gpiod::line::direction::INPUT)
        .set_bias(gpiod::line::bias::DISABLED);

    auto outputSettings = gpiod::line_settings()
        .set_direction(gpiod::line::direction::OUTPUT)
        .set_output_value(gpiod::line::value::INACTIVE);

    gpiod::line::offsets inputLines = { HX711_DATA };
    gpiod::line::offsets outputLines = { HX711_CLOCK };

    auto request = chip.prepare_request()
        .set_consumer("hx711")
        .add_line_settings(inputLines, inputSettings)
        .add_line_settings(outputLines, outputSettings)
        .do_request();

    request.set_value(HX711_CLOCK, gpiod::line::value::INACTIVE);

    std::this_thread::sleep_for(std::chrono::milliseconds(500));

    int invalidReads = 0;
    int32_t hxOffset = hx711ReadValidAverage(request, 10, invalidReads);

    std::ofstream logFile("/home/coletivo/ColetivoMotor/loadcell_motion_log.csv");

    logFile
        << "tempo_ms,"
        << "hx_raw,"
        << "hx_net,"
        << "invalid,"
        << "beep_up,"
        << "beep_down,"
        << "trim_release,"
        << "limit_top,"
        << "limit_bottom,"
        << "movement,"
        << "ap,"
        << "hyd,"
        << "pos"
        << "\n";

    auto startTime = std::chrono::steady_clock::now();

    while (running)
    {
        int32_t hxRaw = hx711ReadValidAverage(request, 3, invalidReads);
        int32_t hxNet = hxRaw - hxOffset;

        g_hxRaw = hxRaw;
        g_hxNet = hxNet;
        g_hxInvalid = invalidReads;

        auto now = std::chrono::steady_clock::now();

        auto tempoMs = std::chrono::duration_cast<std::chrono::milliseconds>(
            now - startTime
        ).count();

        logFile
            << tempoMs << ","
            << hxRaw << ","
            << hxNet << ","
            << invalidReads << ","
            << (g_up.load() ? 1 : 0) << ","
            << (g_down.load() ? 1 : 0) << ","
            << (g_trimRelease.load() ? 1 : 0) << ","
            << (g_limitTop.load() ? 1 : 0) << ","
            << (g_limitBottom.load() ? 1 : 0) << ","
            << g_movement.load() << ","
            << g_autopilotActive.load() << ","
            << g_hydraulicFailure.load() << ","
            << g_transducerPosition.load()
            << "\n";

        logFile.flush();

        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
}

void udpThread()
{
    int sock = socket(AF_INET, SOCK_DGRAM, 0);

    if (sock < 0)
    {
        std::cerr << "Erro ao criar socket UDP" << std::endl;
        return;
    }

    sockaddr_in localAddr{};
    localAddr.sin_family = AF_INET;
    localAddr.sin_addr.s_addr = INADDR_ANY;
    localAddr.sin_port = htons(UDP_RX_PORT);

    if (bind(sock, (sockaddr*)&localAddr, sizeof(localAddr)) < 0)
    {
        std::cerr << "Erro no bind UDP RX" << std::endl;
        close(sock);
        return;
    }

    sockaddr_in dashboardAddr{};
    dashboardAddr.sin_family = AF_INET;
    dashboardAddr.sin_port = htons(UDP_TX_PORT);

    const char* dashboardIp = resolveDashboardIp();
    if (inet_pton(AF_INET, dashboardIp, &dashboardAddr.sin_addr) != 1)
    {
        std::cerr << "[UDP] IP do dashboard invalido: " << dashboardIp << std::endl;
        close(sock);
        return;
    }

    std::cout << "[UDP] Escutando comandos na porta " << UDP_RX_PORT
              << " | Telemetria para " << dashboardIp << ":" << UDP_TX_PORT << std::endl;

    char buffer[256];

    while (running)
    {
        int overrideValue = g_override.load();

        //posTop e posBottom serão enviados no pacote com seus valores reais somente após o fim da calibragem completa (após atingir os finais de curso inferior e superior)
        bool isCalibrated = g_calibrated.load();
        int posTopToSend = isCalibrated ? g_posTop.load() : 0;
        int posBottomToSend = isCalibrated ? g_posBottom.load() : 0;

        char tx[256];
        std::snprintf(
            tx,
            sizeof(tx),
            "C,%d,%d,%d,%d,%d,%d,%d",
            g_up.load() ? 1 : 0,
            g_down.load() ? 1 : 0,
            g_trimRelease.load() ? 1 : 0,
            overrideValue,
            g_hxNet.load(),
            posTopToSend,
            posBottomToSend
        );

        sendto(
            sock,
            tx,
            std::strlen(tx),
            0,
            (sockaddr*)&dashboardAddr,
            sizeof(dashboardAddr)
        );

        fd_set readfds;
        FD_ZERO(&readfds);
        FD_SET(sock, &readfds);

        timeval tv{};
        tv.tv_sec = 0;
        tv.tv_usec = 1000;

        int ready = select(sock + 1, &readfds, nullptr, nullptr, &tv);

        if (ready > 0 && FD_ISSET(sock, &readfds))
        {
            sockaddr_in senderAddr{};
            socklen_t senderLen = sizeof(senderAddr);

            int len = recvfrom(
                sock,
                buffer,
                sizeof(buffer) - 1,
                0,
                (sockaddr*)&senderAddr,
                &senderLen
            );

            if (len > 0)
            {
                buffer[len] = '\0';

                int ap = 0;
                int hyd = 0;
                int transducer = 0;
                int maneuver = 0;
                int previousManeuver = g_maneuverId.load();
                bool parsed = false;

                if (std::sscanf(buffer, "P,%d,%d,%d,%d", &ap, &hyd, &transducer, &maneuver) == 4)
                    parsed = true;
                else if (std::sscanf(buffer, "P,%d,%d,%d", &ap, &hyd, &transducer) == 3)
                {
                    maneuver = ap ? MANEUVER_FULL_STROKE : MANEUVER_NONE;
                    parsed = true;
                }

                if (parsed)
                {
                    g_udpCommandsReceived++;
                    g_autopilotActive = ap;
                    g_hydraulicFailure = hyd;
                    g_transducerPosition = transducer;
                    g_maneuverId = maneuver;

                    if (!ap && previousManeuver != MANEUVER_NONE
                        && g_paPhase == PA_PHASE_EXECUTE)
                    {
                        g_paPhase = PA_PHASE_RETURN_HOME;
                    }
                    else if (maneuver != previousManeuver)
                    {
                        resetPaManeuverState();
                    }
                    else if (!ap && g_paPhase == PA_PHASE_COMPLETE)
                    {
                        resetPaManeuverState();
                    }
                }
            }
        }

        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }

    close(sock);
}

bool stepMotor(gpiod::line_request& request, bool direction)
{
    bool limitTop = (request.get_value(LIMIT_TOP) == gpiod::line::value::INACTIVE);
    bool limitBottom = (request.get_value(LIMIT_BOTTOM) == gpiod::line::value::INACTIVE);

    if (!direction && limitBottom)
        return false;

    if (direction && limitTop)
        return false;

    request.set_value(
        MOTOR_DIR,
        direction ? gpiod::line::value::ACTIVE
                  : gpiod::line::value::INACTIVE
    );

    request.set_value(MOTOR_PUL, gpiod::line::value::ACTIVE);
    std::this_thread::sleep_for(std::chrono::microseconds(STEP_DELAY_US));

    request.set_value(MOTOR_PUL, gpiod::line::value::INACTIVE);
    std::this_thread::sleep_for(std::chrono::microseconds(STEP_DELAY_US));

    return true;
}

bool handleHydraulicFailure(gpiod::line_request& request)
{
    request.set_value(MOTOR_ENA, gpiod::line::value::INACTIVE);

    bool atBottom = (request.get_value(LIMIT_TOP) == gpiod::line::value::INACTIVE);

    if (!atBottom)
    {
        bool stepped = stepMotor(request, true);
        g_movement = stepped ? -1 : 0;
        return stepped;
    }

    g_movement = 0;
    return false;
}

bool runCalibration(gpiod::line_request& request)
{
    if (!waitForDashboardLink())
        return false;

    const int minSpan = readEnvInt("SCCA_MIN_TRANSDUCER_SPAN", 500);
    int minSeen = INT_MAX;
    int maxSeen = INT_MIN;

    std::cout << "\n[CALIB] Iniciando calibragem automática..." << std::endl;
    std::cout << "[CALIB] O transdutor deve variar enquanto o motor atinge os fins de curso."
              << std::endl;

    while (running)
    {
        bool stepped = stepMotor(request, false);
        trackTransducerReading(minSeen, maxSeen);

        if (!stepped)
            break;

        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }

    if (!running)
        return false;

    std::this_thread::sleep_for(std::chrono::milliseconds(200));

    g_posBottom = averageTransducerReading();
    trackTransducerReading(minSeen, maxSeen);

    std::cout << "[CALIB] Fim de curso inferior (LIMIT_BOTTOM) atingido. posBottom = "
              << g_posBottom.load()
              << " | faixa observada [" << minSeen << ", " << maxSeen << "]"
              << std::endl;

    while (running)
    {
        bool stepped = stepMotor(request, true);
        trackTransducerReading(minSeen, maxSeen);

        if (!stepped)
            break;

        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }

    if (!running)
        return false;

    std::this_thread::sleep_for(std::chrono::milliseconds(200));

    g_posTop = averageTransducerReading();
    trackTransducerReading(minSeen, maxSeen);

    std::cout << "[CALIB] Fim de curso superior (LIMIT_TOP) atingido. posTop = "
              << g_posTop.load()
              << " | faixa observada [" << minSeen << ", " << maxSeen << "]"
              << std::endl;

    g_calibTransducerMin = minSeen;
    g_calibTransducerMax = maxSeen;

    int observedSpan = (minSeen == INT_MAX || maxSeen == INT_MIN) ? 0 : (maxSeen - minSeen);
    int snapshotSpan = std::abs(g_posTop.load() - g_posBottom.load());

    if (observedSpan < minSpan)
    {
        std::cerr << "[CALIB] ERRO: Transdutor nao variou o suficiente durante a calibragem."
                  << std::endl;
        std::cerr << "  Faixa observada=" << observedSpan
                  << " (minimo " << minSpan << ")." << std::endl;
        std::cerr << "  Causas comuns:" << std::endl;
        std::cerr << "    - Dashboard sem link UDP com a Raspberry (ping 10.0.0.1)" << std::endl;
        std::cerr << "    - Joystick/transdutor desconectado ou parado em /dev/input/js0" << std::endl;
        std::cerr << "    - Mecanismo do coletivo nao move o sensor do transdutor" << std::endl;
        return false;
    }

    if (g_posTop.load() == g_posBottom.load())
    {
        g_posBottom = minSeen;
        g_posTop = maxSeen;
        snapshotSpan = std::abs(g_posTop.load() - g_posBottom.load());

        std::cout << "[CALIB] Snapshots iguais; usando faixa observada: bottom="
                  << g_posBottom.load() << " top=" << g_posTop.load() << std::endl;
    }

    if (snapshotSpan < minSpan)
    {
        std::cerr << "[CALIB] ERRO: posTop (" << g_posTop.load()
                  << ") e posBottom (" << g_posBottom.load()
                  << ") sem separacao util." << std::endl;
        return false;
    }

    g_calibrated = true;
    resetPaManeuverState();

    std::cout << "[CALIB] Calibragem concluída: posTop (LIMIT_TOP) = "
              << g_posTop.load() << ", posBottom (LIMIT_BOTTOM) = " << g_posBottom.load()
              << std::endl;

    return true;
}

int main()
{
    std::signal(SIGINT, signalHandler);

    std::cout << "COLETIVO - COLETA LOG CELULA DE CARGA + MOVIMENTO" << std::endl;
    std::cout << "Log: /home/coletivo/ColetivoMotor/loadcell_motion_log.csv" << std::endl;

    std::thread hxThread(hx711Thread);
    std::thread udpComThread(udpThread);

    auto chip = gpiod::chip(CHIP_PATH);

    auto inputSettings = gpiod::line_settings()
        .set_direction(gpiod::line::direction::INPUT)
        .set_bias(gpiod::line::bias::PULL_UP);

    auto outputSettings = gpiod::line_settings()
        .set_direction(gpiod::line::direction::OUTPUT)
        .set_output_value(gpiod::line::value::INACTIVE);

    gpiod::line::offsets inputLines = {
        BTN_UP,
        BTN_DOWN,
        BTN_TRIM_RELEASE,
        LIMIT_TOP,
        LIMIT_BOTTOM
    };

    gpiod::line::offsets outputLines = {
        MOTOR_PUL,
        MOTOR_DIR,
        MOTOR_ENA
    };

    auto request = chip.prepare_request()
        .set_consumer("coletivo_motor")
        .add_line_settings(inputLines, inputSettings)
        .add_line_settings(outputLines, outputSettings)
        .do_request();

    request.set_value(MOTOR_ENA, gpiod::line::value::INACTIVE);

    if (!runCalibration(request))
    {
        std::cerr << "[MAIN] Calibragem falhou ou foi interrompida. Encerrando." << std::endl;
        running = false;

        request.set_value(MOTOR_ENA, gpiod::line::value::ACTIVE); //destrava o motor antes de sair

        if (hxThread.joinable())      hxThread.join();
        if (udpComThread.joinable())  udpComThread.join();

        return 1;
    }
    
    while (running)
    {
        bool up = buttonPressed(request, BTN_UP);
        bool down = buttonPressed(request, BTN_DOWN);
        bool trimRelease = buttonPressed(request, BTN_TRIM_RELEASE);

        bool limitTop = buttonPressed(request, LIMIT_TOP);
        bool limitBottom = buttonPressed(request, LIMIT_BOTTOM);
        
        bool hydraulicFailure = g_hydraulicFailure.load() != 0;
        g_limitTop = limitTop;
        g_limitBottom = limitBottom;

        bool trimHold = trimRelease;
        bool paRunning = runAutopilotManeuver(request, trimHold);

        if (hydraulicFailure)
        {
            g_up = false;
            g_down = false;
            g_trimRelease = false;

            bool descending = handleHydraulicFailure(request);

            std::cout
                << "\rUP:0 DOWN:0 TRIM_RELEASE:0"
                << " TOP:" << limitTop
                << " BOTTOM:" << limitBottom
                << " MOV:" << g_movement.load()
                << " HX_RAW:" << g_hxRaw.load()
                << " HX_NET:" << g_hxNet.load()
                << " INVALID:" << g_hxInvalid.load()
                << " AP:0 HYD:1"
                << " POS:" << g_transducerPosition.load()
                << (descending ? " | PANE: descendo ao limite INF  "
                               : " | PANE: travado no limite INF  ")
                << std::flush;

            std::this_thread::sleep_for(std::chrono::milliseconds(10));
            continue;
        }

        if (paRunning)
        {
            std::cout
                << "\rUP:" << (g_up.load() ? 1 : 0)
                << " DOWN:" << (g_down.load() ? 1 : 0)
                << " TRIM_RELEASE:0"
                << " TOP:" << limitTop
                << " BOTTOM:" << limitBottom
                << " MOV:" << g_movement.load()
                << " HX_RAW:" << g_hxRaw.load()
                << " HX_NET:" << g_hxNet.load()
                << " INVALID:" << g_hxInvalid.load()
                << " OVR:" << g_override.load()
                << " AP:" << g_autopilotActive.load()
                << " MAN:" << g_maneuverId.load()
                << " HYD:" << g_hydraulicFailure.load()
                << " POS:" << g_transducerPosition.load()
                << " | PA: manobra " << g_maneuverId.load()
                << " fase " << g_paPhase
                << "                    "
                << std::flush;
            continue;
        }

        if (g_autopilotActive.load()
            && g_maneuverId.load() != MANEUVER_NONE
            && (!trimHold || g_override.load()))
        {
            resetPaManeuverState();
        }

        g_up = up;
        g_down = down;
        g_trimRelease = !trimRelease;

        if (!trimRelease)
        {
            request.set_value(MOTOR_ENA, gpiod::line::value::ACTIVE);
            g_movement = 2;

            std::cout
                << "\rUP:" << up
                << " DOWN:" << down
                << " TRIM_RELEASE:1"
                << " TOP:" << limitTop
                << " BOTTOM:" << limitBottom
                << " MOV:2"
                << " HX_RAW:" << g_hxRaw.load()
                << " HX_NET:" << g_hxNet.load()
                << " INVALID:" << g_hxInvalid.load()
                << " OVR:" << g_override.load()
                << " AP:" << g_autopilotActive.load()
                << " MAN:" << g_maneuverId.load()
                << " HYD:" << g_hydraulicFailure.load()
                << " POS:" << g_transducerPosition.load()
                << " | TRIM RELEASE: motor livre        "
                << std::flush;
        }
        else
        {
            request.set_value(MOTOR_ENA, gpiod::line::value::INACTIVE);

            if (up && !down)
            {
                bool stepped = stepMotor(request, false);
                g_movement = stepped ? 1 : 0;
                updateOverride();

                std::cout
                    << "\rUP:" << up
                    << " DOWN:" << down
                    << " TRIM_RELEASE:0"
                    << " TOP:" << limitTop
                    << " BOTTOM:" << limitBottom
                    << " MOV:" << g_movement.load()
                    << " HX_RAW:" << g_hxRaw.load()
                    << " HX_NET:" << g_hxNet.load()
                    << " INVALID:" << g_hxInvalid.load()
                    << " OVR:" << g_override.load()
                    << " AP:" << g_autopilotActive.load()
                    << " MAN:" << g_maneuverId.load()
                    << " HYD:" << g_hydraulicFailure.load()
                    << " POS:" << g_transducerPosition.load()
                    << (stepped ? " | UP movimentando                 "
                                : " | UP bloqueado: fim de curso INF  ")
                    << std::flush;
            }
            else if (down && !up)
            {
                bool stepped = stepMotor(request, true);
                g_movement = stepped ? -1 : 0;
                updateOverride();

                std::cout
                    << "\rUP:" << up
                    << " DOWN:" << down
                    << " TRIM_RELEASE:0"
                    << " TOP:" << limitTop
                    << " BOTTOM:" << limitBottom
                    << " MOV:" << g_movement.load()
                    << " HX_RAW:" << g_hxRaw.load()
                    << " HX_NET:" << g_hxNet.load()
                    << " INVALID:" << g_hxInvalid.load()
                    << " OVR:" << g_override.load()
                    << " AP:" << g_autopilotActive.load()
                    << " MAN:" << g_maneuverId.load()
                    << " HYD:" << g_hydraulicFailure.load()
                    << " POS:" << g_transducerPosition.load()
                    << (stepped ? " | DOWN movimentando               "
                                : " | DOWN bloqueado: fim de curso SUP")
                    << std::flush;
            }
            else
            {
                g_movement = 0;
                updateOverride();

                std::cout
                    << "\rUP:" << up
                    << " DOWN:" << down
                    << " TRIM_RELEASE:0"
                    << " TOP:" << limitTop
                    << " BOTTOM:" << limitBottom
                    << " MOV:0"
                    << " HX_RAW:" << g_hxRaw.load()
                    << " HX_NET:" << g_hxNet.load()
                    << " INVALID:" << g_hxInvalid.load()
                    << " OVR:" << g_override.load()
                    << " AP:" << g_autopilotActive.load()
                    << " MAN:" << g_maneuverId.load()
                    << " HYD:" << g_hydraulicFailure.load()
                    << " POS:" << g_transducerPosition.load()
                    << " | Motor parado/travado             "
                    << std::flush;

                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            }
        }
    }

    request.set_value(MOTOR_ENA, gpiod::line::value::ACTIVE);

    if (hxThread.joinable())
        hxThread.join();

    if (udpComThread.joinable())
        udpComThread.join();

    std::cout << std::endl << "Programa finalizado." << std::endl;

    return 0;
}