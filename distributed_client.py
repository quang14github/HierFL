import socket
from options import args_parser
from torch.autograd import Variable
import torch
from models.initialize_model import initialize_model
import copy
import io
import argparse
from datasets.get_data import get_dataloaders
import struct


class Client:
    def __init__(self, args, server_host="127.0.0.1", server_port=40000):
        # connect to the head
        self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.client_socket.connect((server_host, server_port))
        # config training
        self.id = None
        self.train_loader = []
        self.val_loader = []
        self.device = args.device
        self.model = initialize_model(args)
        # copy.deepcopy(self.model.shared_layers.state_dict())
        self.receiver_buffer = {}
        self.batch_size = args.batch_size
        self.num_local_update = 0
        # record local update epoch
        self.epoch = 0
        # record the time
        self.clock = []
        self.socket_volumn = args.socket_volumn

    def local_update(self, num_iter):
        itered_num = 0
        loss = 0.0
        end = False
        # the upperbound selected in the following is because it is expected that one local update will never reach 1000
        for epoch in range(1000):
            for data in self.train_loader:
                inputs, labels = data
                inputs = Variable(inputs).to(self.device)
                labels = Variable(labels).to(self.device)
                loss += self.model.optimize_model(
                    input_batch=inputs, label_batch=labels
                )
                itered_num += 1
                if itered_num >= num_iter:
                    end = True
                    # print(f"Iterer number {itered_num}")
                    # self.epoch += 1
                    # self.model.exp_lr_sheduler(epoch=self.epoch)
                    # self.model.print_current_lr()
                    break
            if end:
                break
            # self.epoch += 1
            # self.model.exp_lr_sheduler(epoch=self.epoch)
            # self.model.print_current_lr()
        # print(itered_num)
        # print(f'The {self.epoch}')
        loss /= num_iter
        return loss

    def val_model(self):
        correct = 0.0
        total = 0.0
        with torch.no_grad():
            for data in self.val_loader:
                inputs, labels = data
                inputs = inputs.to(self.device)
                labels = labels.to(self.device)
                outputs = self.model.test_model(input_batch=inputs)
                _, predict = torch.max(outputs, 1)
                total += labels.size(0)
                correct += (predict == labels).sum().item()
        return correct, total

    # send normal message
    def send_msg(self, msg):
        self.client_socket.send(msg.encode("utf-8"))

    # receive normal message
    def receive_msg(self):
        msg = self.client_socket.recv(1024).decode("utf-8")
        return msg

    def connect_to_edge(self, edge_port, host="127.0.0.1"):
        self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.client_socket.connect((host, edge_port))

    def send_data_to_edge(self, loss, correct, total):
        # Serialize the state_dict to a byte stream
        buffer = io.BytesIO()
        torch.save(self.model.shared_layers.state_dict(), buffer)

        # Get the byte stream from the buffer
        state_dict_bytes = buffer.getvalue()

        # Send the size of the state_dict_bytes before sending state_dict_bytes
        state_dict_size = len(state_dict_bytes)
        self.client_socket.sendall(struct.pack("!I", state_dict_size))
        self.client_socket.sendall(state_dict_bytes)

        # Send the loss, correct and total to the edge
        self.client_socket.sendall(
            f"{str(loss)} {str(correct)} {str(total)}".encode("utf-8")
        )
        return None

    def sync_with_edge(self):
        """
        The global has already been stored in the buffer
        :return: None
        """
        # self.model.shared_layers.load_state_dict(self.receiver_buffer)
        self.model.update_model(self.receiver_buffer)
        return None

    def receive_data_from_edge(self):
        while True:
            try:
                state_dict_size = struct.unpack("!I", self.client_socket.recv(4))[0]
                state_dict_bytes = b""
                while len(state_dict_bytes) < state_dict_size:
                    msg = self.client_socket.recv(
                        min(self.socket_volumn, state_dict_size - len(state_dict_bytes))
                    )
                    state_dict_bytes += msg
                    # Load the state_dict from the byte stream
                buffer = io.BytesIO(state_dict_bytes)
                shared_state_dict = torch.load(buffer)
                self.receiver_buffer = shared_state_dict
                # Receive the number of local update
                self.num_local_update = int(
                    self.client_socket.recv(1024).decode("utf-8")
                )
                break
            except:
                pass
        return None

    def disconnect(self):
        self.send_msg("DISCONNECT")

    def build_dataloaders(self, args):
        (train_loaders, val_loaders, _) = get_dataloaders(args)
        self.train_loader = train_loaders[self.id]
        self.val_loader = val_loaders[self.id]

    def start(self):
        # register the client to the server and get the assigned edge's port
        while True:
            # send message to the server to register the client
            self.send_msg("client")
            received_msg = self.receive_msg()
            print(received_msg)
            if received_msg != "":
                self.id = int(received_msg.split(" ")[0])
                edge_port = int(received_msg.split(" ")[1])
                self.build_dataloaders(args)
                print(f"Redirect to edge {edge_port}")
                self.connect_to_edge(edge_port)
                self.send_msg(f"{self.id} {len(self.train_loader.dataset)}")
                break
        # wait for the server to start the training
        while True:
            received_msg = self.receive_msg()
            if received_msg == "start":
                print("start training")
                break
        # training
        for num_comm in range(args.num_communication):
            for num_edgeagg in range(args.num_edge_aggregation):
                print("Start receiving data from edge")
                self.receive_data_from_edge()
                print("Received data from edge")
                self.sync_with_edge()
                loss = self.local_update(num_iter=self.num_local_update)
                print("Start testing")
                correct, total = self.val_model()
                print("Start sending data to edge")
                self.send_data_to_edge(loss=loss, correct=correct, total=total)
                print("Sended data to edge")

        print("Training finished")
        self.client_socket.close()


if __name__ == "__main__":
    args = args_parser()
    client = Client(args)
    client.start()
