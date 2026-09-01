"""
Módulo de Recepção UDP Assíncrono para Dashboard de Sistemas Embarcados.

Implementa comunicação UDP não-bloqueante via QUdpSocket com tratamento
robusto de erros e sinais Qt para integração com threads GUI.
"""

from __future__ import annotations

import logging
import math
import struct
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QHostAddress, QUdpSocket, QAbstractSocket


logger = logging.getLogger(__name__)

MANEUVER_IDS: dict[str, int] = {
    "Manobra 1": 1,
    "Manobra 2": 2,
    "Manobra 3": 3,
    "Manobra 4": 4,
}


@dataclass
class UDPPacket:
    """Estrutura base para pacotes UDP recebidos."""
    timestamp: float
    raw_data: bytes
    source_address: str
    source_port: int


class UDPReceiver(QObject):
    """
    Recebedor UDP assíncrono utilizando QUdpSocket nativo do Qt.

    Não bloqueia a thread principal e emite sinais Qt para notificar
    a GUI sobre novos dados recebidos.

    Sinais:
        - packet_received: Emite (dict) com dados do pacote
        - error_occurred: Emite (str) com descrição do erro
        - connection_status_changed: Emite (bool) True=conectado, False=desconectado
    """

    packet_received = Signal(dict)
    error_occurred = Signal(str)
    connection_status_changed = Signal(bool)

    def __init__(self, host: str = "0.0.0.0", port: int = 5006) -> None:
        """
        Inicializa o receptor UDP.

        Args:
            host: Endereço IP para binding (padrão: 0.0.0.0 para aceitar qualquer interface)
            port: Porta UDP para escuta (padrão: 5006)
        """
        super().__init__()
        self.host = host
        self.port = port
        self.socket: Optional[QUdpSocket] = None
        self._is_connected = False
        self._packet_count = 0
        self._error_count = 0

        # Timer para diagnóstico periódico
        self._diagnostics_timer = QTimer(self)
        self._diagnostics_timer.timeout.connect(self._log_diagnostics)

    def start(self) -> bool:
        """
        Inicia o servidor UDP.

        Retorna:
            bool: True se iniciado com sucesso, False caso contrário.
        """
        try:
            self.socket = QUdpSocket(self)

            # Conectar sinais do socket
            self.socket.readyRead.connect(self._on_ready_read)

            # Bind ao endereço e porta
            if not self.socket.bind(QHostAddress(self.host), self.port):
                error_msg = f"Falha ao fazer bind em {self.host}:{self.port}. Erro: {self.socket.errorString()}"
                logger.error(error_msg)
                self.error_occurred.emit(error_msg)
                return False

            self._is_connected = True
            success_msg = f"Servidor UDP iniciado em {self.host}:{self.port}"
            logger.info(success_msg)
            self.connection_status_changed.emit(True)

            # Iniciar timer de diagnóstico
            self._diagnostics_timer.start(10000)  # A cada 10 segundos

            return True

        except Exception as e:
            error_msg = f"Exceção ao iniciar UDP: {str(e)}"
            logger.error(error_msg, exc_info=True)
            self.error_occurred.emit(error_msg)
            return False

    def stop(self) -> None:
        """Para o servidor UDP e libera recursos."""
        if self._diagnostics_timer.isActive():
            self._diagnostics_timer.stop()

        if self.socket:
            self.socket.close()
            self._is_connected = False
            self.connection_status_changed.emit(False)
            logger.info("Servidor UDP parado")

    def is_connected(self) -> bool:
        """Retorna o estado de conexão do socket."""
        return self._is_connected and self.socket is not None and self.socket.state() == QAbstractSocket.SocketState.BoundState

    def _on_ready_read(self) -> None:
        """Callback disparado quando há dados disponíveis no socket."""
        if not self.socket:
            return

        try:
            while self.socket.hasPendingDatagrams():
                datagram = self.socket.receiveDatagram()
                self._process_datagram(datagram)

        except Exception as e:
            self._error_count += 1
            error_msg = f"Erro ao processar datagrama: {str(e)}"
            logger.error(error_msg, exc_info=True)
            self.error_occurred.emit(error_msg)

    def _process_datagram(self, datagram) -> None:
        """
        Processa um datagrama recebido.

        Tenta parse automático (JSON, estrutura binária) e emite sinal.
        """
        try:
            import time

            # Converter QByteArray para bytes Python
            qbyte_array = datagram.data()
            data = bytes(qbyte_array)
            sender_address = str(datagram.senderAddress().toString())
            sender_port = datagram.senderPort()

            # Tentar parse como JSON
            packet_dict = self._parse_packet(data, sender_address, sender_port)

            # Emitir sinal com dados parseados
            self.packet_received.emit(packet_dict)
            self._packet_count += 1

            logger.debug(
                f"Pacote #{self._packet_count} recebido de {sender_address}:{sender_port} "
                f"({len(data)} bytes)"
            )

        except Exception as e:
            self._error_count += 1
            error_msg = f"Erro ao processar datagrama: {str(e)}"
            logger.error(error_msg, exc_info=True)
            self.error_occurred.emit(error_msg)

    @staticmethod
    def _parse_packet(data: bytes, sender_address: str, sender_port: int) -> dict:
        """
        Parse inteligente do pacote recebido.

        Tenta JSON first, depois fallback para interpretação genérica.

        Args:
            data: Bytes brutos do datagrama
            sender_address: IP do sender
            sender_port: Porta do sender

        Retorna:
            dict com chaves: raw_hex, parsed_data, sender_address, sender_port, timestamp
        """
        import time

        result = {
            "timestamp": time.time(),
            "sender_address": sender_address,
            "sender_port": sender_port,
            "raw_hex": data.hex(),
            "raw_length": len(data),
            "parsed_data": None,
            "parse_format": "hex_raw",
        }

        # Protocolo principal: CSV iniciando com 'C'
        # Formato enviado pelo main.cpp (Raspberry Pi):
        # "C,up,down,trim_release,override,hx_net,pos_top,pos_bottom"
        try:
            decoded_str = data.decode("utf-8")
            parts = [part.strip() for part in decoded_str.strip().split(",")]
            if len(parts) == 8 and parts[0] == "C":
                result["parsed_data"] = {
                    "beep_trim_up": int(parts[1]),
                    "beep_trim_down": int(parts[2]),
                    "trim_release": int(parts[3]),
                    "override": int(parts[4]),
                    "load_cell": int(parts[5]),
                    "pos_top": int(parts[6]),
                    "pos_bottom": int(parts[7]),
                }
                logger.debug(
                    f"beep_trim_up: {result['parsed_data']['beep_trim_up']} | "
                    f"beep_trim_down: {result['parsed_data']['beep_trim_down']} | "
                    f"trim_release: {result['parsed_data']['trim_release']} | "
                    f"override: {result['parsed_data']['override']} | "
                    f"load_cell: {result['parsed_data']['load_cell']} | "
                    f"pos_top: {result['parsed_data']['pos_top']} | "
                    f"pos_bottom: {result['parsed_data']['pos_bottom']}"
                )
                result["parse_format"] = "csv_c"
                return result
        except (ValueError, UnicodeDecodeError):
            pass

        # Tentar interpretação como string ASCII
        try:
            decoded_str = data.decode("utf-8", errors="ignore").strip()
            if decoded_str:
                result["parsed_data"] = {"text": decoded_str}
                result["parse_format"] = "ascii_text"
                return result
        except Exception:
            pass

        # Tentar interpretar como estrutura binária simples (exemplo: float + int)
        if len(data) >= 8:
            try:
                # Exemplo: primeira parte float (4 bytes), segunda parte int (4 bytes)
                float_val = struct.unpack("<f", data[0:4])[0]
                int_val = struct.unpack("<I", data[4:8])[0]
                result["parsed_data"] = {"float_value": float_val, "int_value": int_val}
                result["parse_format"] = "binary_float_int"
                return result
            except struct.error:
                pass

        return result

    def _log_diagnostics(self) -> None:
        """Log periódico de diagnóstico."""
        status = "CONECTADO" if self.is_connected() else "DESCONECTADO"
        logger.info(
            f"UDP Diagnostics - Status: {status} | "
            f"Pacotes: {self._packet_count} | "
            f"Erros: {self._error_count}"
        )


class MockUDPSender(QObject):
    """
    Simulador UDP para testes (emula Raspberry Pi enviando dados de comando coletivo).

    Útil para desenvolvimento e testes sem hardware real.
    """

    def __init__(self, receiver_host: str = "127.0.0.1", receiver_port: int = 5006) -> None:
        super().__init__()
        self.receiver_host = receiver_host
        self.receiver_port = receiver_port
        self.socket = QUdpSocket(self)
        self._send_timer = QTimer(self)
        self._send_timer.timeout.connect(self._send_test_packet)
        self._packet_num = 0

    def start(self, interval_ms: int = 1000) -> None:
        """Inicia envio periódico de pacotes de teste."""
        self._send_timer.start(interval_ms)
        logger.info(f"MockUDPSender iniciado (intervalo: {interval_ms}ms)")

    def stop(self) -> None:
        """Para o envio de pacotes."""
        self._send_timer.stop()
        logger.info("MockUDPSender parado")

    def _send_test_packet(self) -> None:
        """Envia um pacote de teste com dados de COMANDO COLETIVO."""
        import time
        import math

        self._packet_num += 1

        # Simular telemetria de comando coletivo
        position = 30.0 + 20.0 * math.sin(self._packet_num * 3.14159 / 10.0)
        trim_hold = (self._packet_num % 3) != 0
        beep_trim = "NEUTRAL" if (self._packet_num % 2) == 0 else "UP"
        pa_active = True
        hydraulic_failure = False
        pilot_force = 2.0 + 1.5 * math.sin(self._packet_num * 3.14159 / 5.0)

        beep_up = 1 if beep_trim == "UP" else 0
        beep_down = 1 if beep_trim == "DOWN" else 0
        trim_release = 0 if trim_hold else 1
        override = 0 if pa_active else 1
        load_cell = int(round(pilot_force * 10.0))
        pos_top = -32767
        pos_bottom = 32767
        data = (
            f"C,{beep_up},{beep_down},{trim_release},{override},"
            f"{load_cell},{pos_top},{pos_bottom}"
        ).encode("utf-8")
        self.socket.writeDatagram(
            data,
            QHostAddress(self.receiver_host),
            self.receiver_port,
        )

        logger.debug(f"Pacote de teste #{self._packet_num} enviado: Position={position:.1f}%, Force={pilot_force:.2f}kg")


class CommandSender(QObject):
    """
    Envia comandos de manobra para o Raspberry Pi via UDP.
    
    Permite que o dashboard envie comandos de autopiloto (manobras)
    para o Raspberry Pi executar.
    """
    
    command_sent = Signal(dict)  # Sinal quando comando é enviado
    error_occurred = Signal(str)  # Sinal de erro ao enviar
    
    def __init__(
        self,
        receiver_host: str = "127.0.0.1",
        receiver_port: int = 5005,
        send_interval_ms: int = 50,
    ) -> None:
        """
        Inicializa o enviador de comandos.
        
        Args:
            receiver_host: IP do Raspberry Pi (ou 127.0.0.1 para localhost)
            receiver_port: Porta UDP no Raspberry Pi (padrão: 5005 para comandos)
        """
        super().__init__()
        self.receiver_host = receiver_host
        self.receiver_port = receiver_port
        self.socket = QUdpSocket(self)
        self.autopilot_active = False
        self.hydraulic_failure = False
        self.transducer_position = 0
        self.maneuver_id = 0
        self._send_interval_ms = max(50, min(150, int(send_interval_ms)))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._send_periodic_control)

    def set_target(self, receiver_host: str, receiver_port: Optional[int] = None) -> None:
        """Atualiza dinamicamente o destino dos comandos (IP/porta do Raspberry Pi)."""
        self.receiver_host = receiver_host
        if receiver_port is not None:
            self.receiver_port = receiver_port
        logger.info(f"Destino de comandos atualizado para {self.receiver_host}:{self.receiver_port}")

    def set_send_interval_ms(self, interval_ms: int) -> None:
        """Ajusta intervalo de envio periódico no range 50-150 ms."""
        self._send_interval_ms = max(50, min(150, int(interval_ms)))
        if self._timer.isActive():
            self._timer.start(self._send_interval_ms)

    def start_stream(self) -> None:
        """Inicia streaming periódico do pacote de controle 'P'."""
        if not self._timer.isActive():
            self._timer.start(self._send_interval_ms)
            logger.info(
                f"Stream de controle UDP iniciado ({self._send_interval_ms} ms) para "
                f"{self.receiver_host}:{self.receiver_port}"
            )

    def stop_stream(self) -> None:
        """Para streaming periódico do pacote de controle 'P'."""
        if self._timer.isActive():
            self._timer.stop()

    def set_control_state(
        self,
        autopilot_active: Optional[bool] = None,
        hydraulic_failure: Optional[bool] = None,
        transducer_position: Optional[int] = None,
        maneuver_id: Optional[int] = None,
    ) -> None:
        if autopilot_active is not None:
            self.autopilot_active = bool(autopilot_active)
        if hydraulic_failure is not None:
            self.hydraulic_failure = bool(hydraulic_failure)
        if transducer_position is not None:
            self.transducer_position = int(transducer_position)
        if maneuver_id is not None:
            self.maneuver_id = int(maneuver_id)

    def _build_control_payload(self) -> bytes:
        return (
            f"P,{int(self.autopilot_active)},{int(self.hydraulic_failure)},"
            f"{int(self.transducer_position)},{int(self.maneuver_id)}"
        ).encode("utf-8")

    def _send_periodic_control(self) -> None:
        self._send_control_packet()

    def _send_control_packet(self) -> bool:
        try:
            data = self._build_control_payload()
            sent = self.socket.writeDatagram(
                data,
                QHostAddress(self.receiver_host),
                self.receiver_port,
            )
            if sent == len(data):
                self.command_sent.emit(
                    {
                        "command_type": "control_stream",
                        "autopilot_active": self.autopilot_active,
                        "hydraulic_failure": self.hydraulic_failure,
                        "transducer_position": self.transducer_position,
                        "maneuver_id": self.maneuver_id,
                    }
                )
                return True

            error_msg = f"Falha ao enviar controle: enviados {sent} de {len(data)} bytes"
            logger.error(error_msg)
            self.error_occurred.emit(error_msg)
            return False
        except Exception as e:
            error_msg = f"Exceção ao enviar controle: {str(e)}"
            logger.error(error_msg)
            self.error_occurred.emit(error_msg)
            return False
    
    def send_maneuver_command(
        self,
        maneuver_name: str,
        parameters: dict | None = None,
        action: str = "start",
    ) -> bool:
        """
        Envia um comando de manobra para o Raspberry Pi.
        
        Args:
            maneuver_name: Nome da manobra (ex: "Circuito Classico", "8 Normais", etc)
            parameters: Dicionário opcional com parâmetros adicionais
            
        Returns:
            bool: True se enviado com sucesso, False caso contrário
        """
        try:
            maneuver_id = MANEUVER_IDS.get(maneuver_name, 0)
            del parameters
            self.set_control_state(
                autopilot_active=(str(action).lower() != "stop"),
                maneuver_id=maneuver_id if str(action).lower() != "stop" else 0,
            )
            return self._send_control_packet()
        except Exception as e:
            error_msg = f"Exceção ao enviar comando: {str(e)}"
            logger.error(error_msg)
            self.error_occurred.emit(error_msg)
            return False

    def send_maneuver_stop(self, maneuver_name: str) -> bool:
        """Envia comando para parar/cancelar uma manobra."""
        return self.send_maneuver_command(maneuver_name=maneuver_name, parameters={}, action="stop")

    def send_system_command(self, command: str, value: object) -> bool:
        """Envia comando de sistema (ex.: set_hydraulic_failure) para o Raspberry Pi."""
        try:
            if command == "set_hydraulic_failure":
                self.set_control_state(hydraulic_failure=bool(value))
                return self._send_control_packet()

            error_msg = f"Comando de sistema não suportado no protocolo CSV: {command}"
            logger.error(error_msg)
            self.error_occurred.emit(error_msg)
            return False

        except Exception as e:
            error_msg = f"Exceção ao enviar comando de sistema: {str(e)}"
            logger.error(error_msg)
            self.error_occurred.emit(error_msg)
            return False

    def send_control_command(self, transducer_position: int, trim_hold: bool, beep_trim: str = "NEUTRAL") -> bool:
        """
        Envia um comando de controle direto para o Raspberry Pi.

        Args:
            transducer_position: Posição desejada em unidades brutas do transdutor
            trim_hold: Se deve ativar o hold do trim
            beep_trim: Direção do beep ("UP", "DOWN", "NEUTRAL")

        Returns:
            bool: True se enviado com sucesso
        """
        del trim_hold
        del beep_trim
        self.set_control_state(
            transducer_position=int(round(float(transducer_position))),
        )
        return self._send_control_packet()


class MockRaspberryAutopilot(QObject):
    """
    Simula um Raspberry Pi local:
    - recebe comandos UDP (porta de comando)
    - envia telemetria UDP (porta do dashboard)
    """

    status_changed = Signal(bool)
    error_occurred = Signal(str)

    def __init__(
        self,
        command_host: str = "127.0.0.1",
        command_port: int = 5005,
        telemetry_host: str = "127.0.0.1",
        telemetry_port: int = 5006,
    ) -> None:
        super().__init__()
        self.command_host = command_host
        self.command_port = command_port
        self.telemetry_host = telemetry_host
        self.telemetry_port = telemetry_port

        self.command_socket: Optional[QUdpSocket] = None
        self.telemetry_socket = QUdpSocket(self)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self._running = False

        self.position_percent = 8.0
        self.trim_hold = True
        self.beep_trim = "NEUTRAL"
        self.pa_active = True
        self.hydraulic_failure = False
        self.pilot_force_kg = 0.0
        self.udp_connected = True
        self.usb_connected = True
        self.selected_maneuver = "Manobra 1"
        self.maneuver_active = False
        self.maneuver_state = "IDLE"
        self._t = 0.0
        self._dt = 0.1
        self._transducer_position = int(self.position_percent * 100.0)
        self.maneuver_id = 0
        self._pa_direction = 1
        self._pa_step_index = 0
        self._pa_step_hold = 0.0
        self.pos_top = -32767
        self.pos_bottom = 0
        self.calibrated = True

    def start(self, interval_ms: int = 50) -> bool:
        try:
            self.command_socket = QUdpSocket(self)
            if not self.command_socket.bind(QHostAddress(self.command_host), self.command_port):
                msg = (
                    f"MockRaspberry bind falhou em {self.command_host}:{self.command_port} "
                    f"- {self.command_socket.errorString()}"
                )
                logger.error(msg)
                self.error_occurred.emit(msg)
                return False

            self.command_socket.readyRead.connect(self._on_command_ready_read)
            self.timer.start(interval_ms)
            self._running = True
            logger.info(
                f"MockRaspberry ativo: cmd {self.command_host}:{self.command_port} -> "
                f"telem {self.telemetry_host}:{self.telemetry_port}"
            )
            self.status_changed.emit(True)
            return True
        except Exception as e:
            msg = f"Falha ao iniciar MockRaspberry: {e}"
            logger.error(msg)
            self.error_occurred.emit(msg)
            return False

    def stop(self) -> None:
        if self.timer.isActive():
            self.timer.stop()
        if self.command_socket:
            self.command_socket.close()
            self.command_socket = None
        self._running = False
        self.status_changed.emit(False)
        logger.info("MockRaspberry parado")

    def _on_command_ready_read(self) -> None:
        if not self.command_socket:
            return
        while self.command_socket.hasPendingDatagrams():
            dg = self.command_socket.receiveDatagram()
            payload = bytes(dg.data()).decode("utf-8", errors="ignore").strip()
            try:
                parts = [part.strip() for part in payload.split(",")]
                if len(parts) == 5 and parts[0] == "P":
                    autopilot_active = bool(int(parts[1]))
                    hydraulic_failure = bool(int(parts[2]))
                    transducer_position = int(parts[3])
                    maneuver_id = int(parts[4])
                elif len(parts) == 4 and parts[0] == "P":
                    autopilot_active = bool(int(parts[1]))
                    hydraulic_failure = bool(int(parts[2]))
                    transducer_position = int(parts[3])
                    maneuver_id = 1 if autopilot_active else 0
                else:
                    continue
            except Exception:
                continue

            previous_maneuver = self.maneuver_id
            self.pa_active = autopilot_active
            self.hydraulic_failure = hydraulic_failure
            self._transducer_position = transducer_position
            self.maneuver_id = maneuver_id
            self.selected_maneuver = next(
                (name for name, mid in MANEUVER_IDS.items() if mid == maneuver_id),
                "Manobra 1",
            )
            if maneuver_id != previous_maneuver:
                self._t = 0.0
                self._pa_step_index = 0
                self._pa_step_hold = 0.0
            self.maneuver_active = autopilot_active
            self.maneuver_state = "RUNNING" if autopilot_active else "IDLE"
            self.trim_hold = not autopilot_active
            if self.hydraulic_failure:
                self.maneuver_active = False
                self.maneuver_state = "ABORTED"
                self.trim_hold = True

    def _profile(self, name: str, t: float) -> float:
        if name == "Manobra 2":
            return 45.0 + 20.0 * math.sin(0.6 * t)
        if name == "Manobra 3":
            return 50.0 + 16.0 * math.sin(0.9 * t + 1.2)
        if name == "Manobra 4":
            phase = int((t // 1.5) % 4)
            levels = [20.0, 40.0, 60.0, 35.0]
            return levels[phase]
        return 40.0 + 18.0 * math.sin(0.5 * t)

    def _simulate_maneuver_percent(self, maneuver_id: int, t: float) -> float:
        if maneuver_id == 1:
            phase = int((t // 4.0) % 6)
            if phase % 2 == 0:
                return min(100.0, (t % 4.0) / 4.0 * 100.0)
            return max(0.0, 100.0 - ((t % 4.0) / 4.0 * 100.0))
        if maneuver_id == 2:
            phase = int((t // 5.0) % 4)
            if phase % 2 == 0:
                return min(100.0, (t % 5.0) / 1.5 * 100.0)
            return max(0.0, 100.0 - ((t % 5.0) / 3.5 * 100.0))
        if maneuver_id == 3:
            return 30.0 + 15.0 * math.sin(1.2 * t)
        if maneuver_id == 4:
            levels = [20.0, 45.0, 75.0, 30.0]
            if self._pa_step_hold <= 0.0:
                self._pa_step_hold = t
            if (t - self._pa_step_hold) >= 2.0:
                self._pa_step_index = (self._pa_step_index + 1) % 4
                self._pa_step_hold = t
            return levels[self._pa_step_index]
        return self.position_percent

    def _tick(self) -> None:
        import time

        if self.hydraulic_failure:
            self.position_percent = 0.0
            self.pilot_force_kg = 0.0
            self.beep_trim = "DOWN"
            self.trim_hold = True
        elif self.maneuver_active and self.maneuver_id > 0:
            prev = self.position_percent
            target_percent = max(
                15.0,
                min(85.0, self._simulate_maneuver_percent(self.maneuver_id, self._t)),
            )
            self.position_percent += (target_percent - self.position_percent) * 0.35
            slope = self.position_percent - prev
            if slope > 0.05:
                self.beep_trim = "UP"
            elif slope < -0.05:
                self.beep_trim = "DOWN"
            else:
                self.beep_trim = "NEUTRAL"
            self.pilot_force_kg = max(0.0, min(8.0, abs(slope) * 0.8 + 1.2))
            self.trim_hold = True
            self._t += self._dt
        else:
            self.position_percent = self.position_percent
            self.pilot_force_kg = max(0.0, self.pilot_force_kg * 0.95)
            if self.maneuver_state not in ("ABORTED",):
                self.maneuver_state = "IDLE"
            self.beep_trim = "NEUTRAL"

        beep_up = 1 if self.beep_trim == "UP" else 0
        beep_down = 1 if self.beep_trim == "DOWN" else 0
        trim_release = 0 if self.trim_hold else 1
        override = 0 if self.pa_active else 1
        load_cell = int(round(self.pilot_force_kg * 10.0))
        pos_top_to_send = self.pos_top if self.calibrated else 0
        pos_bottom_to_send = self.pos_bottom if self.calibrated else 0
        data = (
            f"C,{beep_up},{beep_down},{trim_release},{override},"
            f"{load_cell},{pos_top_to_send},{pos_bottom_to_send}"
        ).encode("utf-8")
        self.telemetry_socket.writeDatagram(
            data,
            QHostAddress(self.telemetry_host),
            self.telemetry_port,
        )